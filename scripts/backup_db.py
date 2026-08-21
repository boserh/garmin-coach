"""OPS-02 · Online, consistent backup of the SQLite ``garmin.db`` with rotation.

The whole value of this system — a year of ``daily_metrics``, run series, plans,
cost logs — lives in one SQLite file on a Raspberry Pi SD card. SD corruption is
the single most likely Pi failure, and a bad ``alembic upgrade`` on the live DB is
the second. This script makes a *consistent* copy while the bot and web are still
writing (a plain ``cp`` can tear a page), rotated so old copies don't pile up.

Usage::

    ./venv/bin/python -m scripts.backup_db                 # → backups/garmin-YYYY-MM-DD.db
    ./venv/bin/python -m scripts.backup_db --dir /mnt/usb  # backups elsewhere
    ./venv/bin/python -m scripts.backup_db --rsync-dest user@host:/backups/
    ./venv/bin/python -m scripts.backup_db --pre-migration # rollback copy before alembic

Notes / pitfalls (see docs/backlog/archive/OPS-02-sqlite-backups.md):

- **Not** ``cp``: ``VACUUM INTO`` (SQLite ≥ 3.27) takes a read lock and writes a
  clean, defragmented copy; the fallback is the online backup API. Both are safe on
  a live DB.
- The DB path comes from ``settings.DATABASE_URL`` (not hard-coded ``./garmin.db``)
  so a relocated DB is still found. Only ``sqlite`` URLs are supported — a Postgres
  deployment (PERF-03) would use ``pg_dump`` instead.
- **Off-SD copy matters**: a backup sitting on the same SD card dies with it. Pass
  ``--rsync-dest`` (or copy ``backups/`` off-box by other means) so the rotated set
  lives elsewhere. The rsync mirrors the whole rotated dir with ``--delete``, so the
  off-SD copy stays bounded (7 daily + 4 weekly) instead of growing forever. A local
  destination is verified to be on a **different filesystem** afterwards
  (``_check_off_sd``): an unmounted mount point is just a writable directory on the card,
  and rsync into it succeeds while quietly making the copy worthless. ``--allow-same-fs``
  opts out.
- The Fernet-encrypted credentials in the DB are useless without ``APP_SECRET_KEY``,
  so the DB copy is safe to store alongside untrusted hosts — but that also means a
  restored backup can't decrypt creds unless ``.env``/``APP_SECRET_KEY`` is backed up
  **separately** (password manager / encrypted file). Do that once, out of band.
- **OPS-08 freshness marker**: a successful run writes ``backups/last_ok.json``
  (ts/path/size/rsync_ok) via ``_write_marker`` — the one thing ``app.backup_status``
  reads to answer "is the backup actually still happening" from ``/status`` and the
  morning tick, without either of them touching the SD card themselves. Only a
  successful ``make_backup`` refreshes it; a failed rsync is recorded on it
  (``rsync_ok: false``) rather than skipping the write, since the *local* backup is
  still real and fresh even when the off-SD copy failed. The marker also carries
  ``rsync_error`` — the reason, one line — because ``rsync_ok: false`` on its own only
  tells the operator that something is wrong, not what to go fix.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from app.backup_status import MARKER_NAME
from app.core.config import settings

_BACKUP_RE = re.compile(r"^garmin-(\d{4}-\d{2}-\d{2})\.db$")


def sqlite_path_from_url(url: str) -> Path:
    """Extract the on-disk file path from a SQLAlchemy SQLite URL.

    Handles ``sqlite:///relative.db``, ``sqlite+aiosqlite:///./garmin.db`` and
    ``sqlite:////absolute/path.db``. Raises for non-SQLite URLs.
    """
    if not url.startswith("sqlite"):
        raise ValueError(
            f"backup_db only supports sqlite DATABASE_URL, got {url!r}. "
            "For Postgres use pg_dump (see PERF-03)."
        )
    # Everything after the '://' scheme separator is the path (leading slashes vary:
    # '///rel' → 'rel', '////abs' → '/abs').
    _, _, tail = url.partition("://")
    path = tail.lstrip("/")
    if url.count("/") >= 4 and "////" in url:
        path = "/" + path  # absolute form sqlite:////abs/path.db
    if not path or path == ":memory:":
        raise ValueError(f"cannot back up an in-memory / empty SQLite URL: {url!r}")
    return Path(path)


def make_backup(src: Path, dest: Path) -> None:
    """Write a consistent copy of ``src`` to ``dest`` (online-safe)."""
    if dest.exists():
        dest.unlink()  # VACUUM INTO refuses to overwrite an existing file
    con = sqlite3.connect(str(src))
    try:
        try:
            con.execute("VACUUM INTO ?", (str(dest),))
        except sqlite3.OperationalError:
            # SQLite < 3.27 has no VACUUM INTO — fall back to the online backup API.
            with sqlite3.connect(str(dest)) as dst:
                con.backup(dst)
    finally:
        con.close()


def _keep_set(backups: list[tuple[date, Path]], *, daily: int, weekly: int) -> set[Path]:
    """Which backups to keep: the ``daily`` most-recent, plus the most-recent one from
    each of the ``weekly`` most-recent ISO weeks."""
    by_recent = sorted(backups, key=lambda t: t[0], reverse=True)
    keep = {p for _, p in by_recent[:daily]}

    seen_weeks: dict[tuple[int, int], Path] = {}
    for d, p in by_recent:
        wk = d.isocalendar()[:2]  # (iso_year, iso_week)
        if wk not in seen_weeks:
            seen_weeks[wk] = p  # first seen = most recent in that week
    for wk in sorted(seen_weeks, reverse=True)[:weekly]:
        keep.add(seen_weeks[wk])
    return keep


def rotate(backup_dir: Path, *, daily: int = 7, weekly: int = 4) -> list[Path]:
    """Delete stale backups, keeping ``daily`` dailies + ``weekly`` weeklies.

    Returns the list of files removed.
    """
    found: list[tuple[date, Path]] = []
    for p in backup_dir.glob("garmin-*.db"):
        m = _BACKUP_RE.match(p.name)
        if m:
            found.append((date.fromisoformat(m.group(1)), p))
    keep = _keep_set(found, daily=daily, weekly=weekly)
    removed = []
    for _, p in found:
        if p not in keep:
            p.unlink()
            removed.append(p)
    return removed


class RsyncFailed(RuntimeError):
    """The off-SD copy failed, with a reason short enough to store in the marker and
    show on ``/status``. ``CalledProcessError`` alone reads as "exit status 23" and
    leaves the operator to ssh in and guess."""


# rsync exit codes a retry can plausibly fix: socket/file I/O, a protocol hiccup over
# ssh, a source file that vanished mid-copy, and the two timeouts. Everything else —
# 1/2/3 (bad invocation, incompatible remote, missing source), 13 (dest missing), 23
# (partial transfer: here almost always a read-only or unwritable destination, since
# rotation has already finished and nothing else touches backups/) — repeats identically,
# and retrying only delays the honest failure by 15s.
_TRANSIENT_RSYNC_CODES = frozenset({10, 11, 12, 24, 30, 35})
# ...but the code alone is not enough: an unwritable destination and a flaky USB both
# exit 11 ("error in file IO"), and the first one repeats forever. rsync names the cause
# on stderr, so read it — a mount that isn't there or isn't writable is a state only a
# human can change, and burning 15s of the nightly window on it teaches nothing.
_PERMANENT_RSYNC_CAUSES = (
    "permission denied",            # incl. ssh "Permission denied (publickey)"
    "read-only file system",
    "no space left",
    "disk quota exceeded",
    "no such file or directory",
    "operation not permitted",
    "host key verification failed",
)
_RSYNC_RETRIES = 2          # extra attempts after the first
_RSYNC_BACKOFF_S = 5.0      # doubled per retry: 5s, 10s
# A hung rsync (unplugged USB still mounted, dead ssh) must not park the oneshot unit
# forever: systemd's start timeout is infinite for Type=oneshot, and while the unit is
# "starting" the next night's timer fires into a no-op — backups stop with no marker
# and no error at all.
_RSYNC_TIMEOUT_S = 900


def _stderr_of(exc: Exception) -> str:
    return " ".join(str(getattr(exc, s, "") or "") for s in ("stderr", "stdout"))


def _is_permanent(exc: Exception) -> bool:
    """True when rsync's own message names a cause no retry can change."""
    err = _stderr_of(exc).lower()
    return any(cause in err for cause in _PERMANENT_RSYNC_CAUSES)


def _rsync_reason(exc: Exception) -> str:
    """A one-line, marker-sized reason. rsync says WHY on stderr ("No such file or
    directory", "Read-only file system", "Permission denied") — that line is the whole
    difference between a fixable report and a bare boolean."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"rsync timed out after {_RSYNC_TIMEOUT_S}s"
    if isinstance(exc, FileNotFoundError):
        return "rsync not installed (command not found)"
    if isinstance(exc, subprocess.CalledProcessError):
        err = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        # rsync prints the failing path first and its own summary last; the first lines
        # carry the cause, so keep those rather than the tail.
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()][:2]
        detail = " | ".join(lines)
        return f"rsync exit {exc.returncode}" + (f": {detail}" if detail else "")
    return f"{type(exc).__name__}: {exc}"[:300]


def _is_local_dest(dest: str) -> bool:
    """True for a plain filesystem path — not ``user@host:/path``, ``host:/path`` or
    ``rsync://host/module``. Only a local destination can be checked for being on the
    same disk as the thing it is supposed to be protecting."""
    if "://" in dest:
        return False
    head = dest.split("/", 1)[0]
    return ":" not in head


def _device_of(path: Path) -> int | None:
    """``st_dev`` of ``path``, or ``None`` if it cannot be stat'ed.

    Deliberately no walk up to an existing ancestor: this runs *after* a successful
    rsync, so the destination exists — and judging a missing path by its parent's device
    would fail a perfectly good remote-ish setup on a guess."""
    try:
        return path.stat().st_dev
    except OSError:
        return None


def _check_off_sd(backup_dir: Path, dest: str) -> None:
    """The failure this whole feature exists to survive is the SD card dying — so a copy
    that lands on that same card is not a backup, it is a false green.

    A mount point is an ordinary directory when nothing is mounted on it: if the USB
    stick is absent (or silently dropped after a reset) and that directory happens to be
    writable, rsync succeeds, the marker says ``rsync_ok: true``, ``/status`` is green,
    and the entire "off-SD" set sits on the card it was meant to outlive. Compare
    ``st_dev`` instead of trusting the path — that is exactly what "a different disk"
    means, and it costs two stat calls. Remote destinations are trivially off-SD and are
    not checked."""
    if not _is_local_dest(dest):
        return
    dest_dev, src_dev = _device_of(Path(dest)), _device_of(backup_dir)
    if dest_dev is None or src_dev is None or dest_dev != src_dev:
        return
    # Verdict and action first, path last: the reason is capped (300 in the marker, 200
    # on the way out) and a long destination path would otherwise eat the fix.
    raise RsyncFailed(
        f"not an off-SD copy — nothing mounted at the destination, it is the same "
        f"filesystem as the backups; check findmnt / mount -a: {dest}"
    )


def _rsync_once(backup_dir: Path, dest: str) -> None:
    # Mirror the whole *rotated* backup dir (trailing slash → sync contents) with
    # --delete, so the off-SD copy self-prunes in lockstep with rotation instead of
    # growing forever — otherwise a nightly single-file copy fills the USB stick.
    subprocess.run(
        ["rsync", "-a", "--delete", f"{backup_dir}/", dest],
        check=True, capture_output=True, text=True, timeout=_RSYNC_TIMEOUT_S,
    )


def _rsync(backup_dir: Path, dest: str, *, check_off_sd: bool = True) -> None:
    """Copy the rotated set off the SD card, retrying only what a retry can fix, then
    verify the copy actually left the card.

    Raises :class:`RsyncFailed` with a human-readable reason — the caller records it in
    the marker and re-raises, so cron/systemd still see a nonzero exit."""
    for attempt in range(_RSYNC_RETRIES + 1):
        try:
            _rsync_once(backup_dir, dest)
            if check_off_sd:
                _check_off_sd(backup_dir, dest)
            return
        except RsyncFailed:
            raise                      # the same-filesystem verdict: no retry can fix it
        except Exception as exc:  # noqa: BLE001 — every failure becomes RsyncFailed below
            code = getattr(exc, "returncode", None)
            transient = (
                (isinstance(exc, subprocess.TimeoutExpired)
                 or code in _TRANSIENT_RSYNC_CODES)
                and not _is_permanent(exc)
            )
            reason = _rsync_reason(exc)
            if attempt < _RSYNC_RETRIES and transient:
                backoff = _RSYNC_BACKOFF_S * (2 ** attempt)
                print(f"rsync transient failure ({reason}) — retry "
                      f"{attempt + 1}/{_RSYNC_RETRIES} in {backoff:.0f}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise RsyncFailed(reason) from exc


# A rollback copy taken immediately before `alembic upgrade head` (see scripts/migrate.py).
# Named apart from the rotated daily set on purpose — twice:
#   * it must not overwrite ``garmin-<today>.db``, or a deploy would replace last night's
#     clean copy with one taken seconds before the very migration it protects against;
#   * ``rotate()``'s regex only matches the daily name, so these would otherwise never be
#     pruned — hence their own small retention below.
PREMIGRATE_PREFIX = "garmin-premigrate-"
PREMIGRATE_KEEP = 3


def pre_migration_backup(backup_dir: Path, *, keep: int = PREMIGRATE_KEEP,
                         now: datetime | None = None) -> Path:
    """A rollback copy taken right before a migration, kept apart from the nightly set.

    Deliberately does **not** write the OPS-08 freshness marker. That marker answers one
    question — "is the SCHEDULED backup still happening, and is it still landing off the
    SD card" — and an ad-hoc copy taken by a deploy must not answer it: deploys are
    frequent enough to keep `age_hours` green for weeks after the nightly timer died, and
    writing `rsync_ok: null` here would erase a recorded `rsync_ok: false`, silencing the
    one warning that says the off-SD copy is broken. Doesn't rsync either, for the same
    reason: this copy exists to survive the next 30 seconds, not the SD card.
    """
    src = sqlite_path_from_url(settings.DATABASE_URL)
    if not src.exists():
        raise FileNotFoundError(f"database file not found: {src}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%dT%H%M%S")
    dest = backup_dir / f"{PREMIGRATE_PREFIX}{stamp}.db"
    make_backup(src, dest)
    for old in sorted(backup_dir.glob(f"{PREMIGRATE_PREFIX}*.db"), reverse=True)[keep:]:
        old.unlink()
    return dest


def _write_marker(backup_dir: Path, dest: Path, *, rsync_ok: bool | None,
                  rsync_error: str | None = None) -> None:
    """OPS-08: record that a backup just succeeded, so a freshness check elsewhere
    (``/status``, the morning tick) can tell "keeps happening" from "happened once,
    a year ago". Written only on a successful ``make_backup`` — a failed backup must
    never refresh this, or the freshness monitor would report a dead backup as fine.
    Atomic replace so a reader never observes a half-written file."""
    marker = {
        "ts": time.time(),
        "path": str(dest),
        "size": dest.stat().st_size,
        "rsync_ok": rsync_ok,
        # Why it failed, not just that it did — the marker is the only thing /status and
        # the morning DM read, so a reason left in the systemd journal is a reason nobody
        # sees until they ssh in.
        "rsync_error": rsync_error[:300] if rsync_error else None,
    }
    tmp = backup_dir / f"{MARKER_NAME}.tmp"
    tmp.write_text(json.dumps(marker))
    tmp.replace(backup_dir / MARKER_NAME)


def run(
    backup_dir: Path,
    *,
    daily: int = 7,
    weekly: int = 4,
    rsync_dest: str | None = None,
    on_date: date | None = None,
    check_off_sd: bool = True,
) -> Path:
    src = sqlite_path_from_url(settings.DATABASE_URL)
    if not src.exists():
        raise FileNotFoundError(f"database file not found: {src}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (on_date or datetime.now().date()).isoformat()
    dest = backup_dir / f"garmin-{stamp}.db"
    make_backup(src, dest)
    rotate(backup_dir, daily=daily, weekly=weekly)

    # OPS-08: an rsync failure is recorded in the marker, separately from the backup
    # itself — the local backup is real and fresh even when the off-SD copy failed
    # (dead USB stick, network blip), so the marker must say both things independently.
    rsync_ok: bool | None = None
    rsync_error: Exception | None = None
    if rsync_dest:
        try:
            # mirror the rotated set, not just today's file — then check it left the card
            _rsync(backup_dir, rsync_dest, check_off_sd=check_off_sd)
            rsync_ok = True
        except Exception as exc:  # noqa: BLE001 — captured in the marker, re-raised below
            rsync_ok = False
            rsync_error = exc

    _write_marker(backup_dir, dest, rsync_ok=rsync_ok,
                  rsync_error=str(rsync_error) if rsync_error is not None else None)
    if rsync_error is not None:
        raise rsync_error
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Online SQLite backup with rotation (OPS-02).")
    ap.add_argument("--dir", default="backups", help="backup directory (default: backups/)")
    ap.add_argument("--daily", type=int, default=7, help="daily copies to keep")
    ap.add_argument("--weekly", type=int, default=4, help="weekly copies to keep")
    ap.add_argument("--rsync-dest", help="rsync the fresh backup here (off-SD copy)")
    ap.add_argument(
        "--pre-migration", action="store_true",
        help="take a rollback copy for an imminent migration (own name + retention, no "
             "freshness marker, no rsync) instead of a rotated nightly backup",
    )
    ap.add_argument(
        "--allow-same-fs", action="store_true",
        help="accept an --rsync-dest on the same filesystem as --dir (default: refuse — "
             "an unmounted mount point makes the off-SD copy land back on the SD card)",
    )
    args = ap.parse_args(argv)
    try:
        if args.pre_migration:
            dest = pre_migration_backup(Path(args.dir))
        else:
            dest = run(
                Path(args.dir), daily=args.daily, weekly=args.weekly,
                rsync_dest=args.rsync_dest, check_off_sd=not args.allow_same_fs,
            )
    except Exception as exc:  # noqa: BLE001 — a cron line wants a clear message + nonzero exit
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup ok → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
