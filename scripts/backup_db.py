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

Notes / pitfalls (see docs/backlog/OPS-02):

- **Not** ``cp``: ``VACUUM INTO`` (SQLite ≥ 3.27) takes a read lock and writes a
  clean, defragmented copy; the fallback is the online backup API. Both are safe on
  a live DB.
- The DB path comes from ``settings.DATABASE_URL`` (not hard-coded ``./garmin.db``)
  so a relocated DB is still found. Only ``sqlite`` URLs are supported — a Postgres
  deployment (PERF-03) would use ``pg_dump`` instead.
- **Off-SD copy matters**: a backup sitting on the same SD card dies with it. Pass
  ``--rsync-dest`` (or copy ``backups/`` off-box by other means) so the rotated set
  lives elsewhere. The rsync mirrors the whole rotated dir with ``--delete``, so the
  off-SD copy stays bounded (7 daily + 4 weekly) instead of growing forever.
- The Fernet-encrypted credentials in the DB are useless without ``APP_SECRET_KEY``,
  so the DB copy is safe to store alongside untrusted hosts — but that also means a
  restored backup can't decrypt creds unless ``.env``/``APP_SECRET_KEY`` is backed up
  **separately** (password manager / encrypted file). Do that once, out of band.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

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


def _rsync(backup_dir: Path, dest: str) -> None:
    # Mirror the whole *rotated* backup dir (trailing slash → sync contents) with
    # --delete, so the off-SD copy self-prunes in lockstep with rotation instead of
    # growing forever — otherwise a nightly single-file copy fills the USB stick.
    subprocess.run(["rsync", "-a", "--delete", f"{backup_dir}/", dest], check=True)


def run(
    backup_dir: Path,
    *,
    daily: int = 7,
    weekly: int = 4,
    rsync_dest: str | None = None,
    on_date: date | None = None,
) -> Path:
    src = sqlite_path_from_url(settings.DATABASE_URL)
    if not src.exists():
        raise FileNotFoundError(f"database file not found: {src}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (on_date or datetime.now().date()).isoformat()
    dest = backup_dir / f"garmin-{stamp}.db"
    make_backup(src, dest)
    rotate(backup_dir, daily=daily, weekly=weekly)
    if rsync_dest:
        _rsync(backup_dir, rsync_dest)  # mirror the rotated set, not just today's file
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Online SQLite backup with rotation (OPS-02).")
    ap.add_argument("--dir", default="backups", help="backup directory (default: backups/)")
    ap.add_argument("--daily", type=int, default=7, help="daily copies to keep")
    ap.add_argument("--weekly", type=int, default=4, help="weekly copies to keep")
    ap.add_argument("--rsync-dest", help="rsync the fresh backup here (off-SD copy)")
    args = ap.parse_args(argv)
    try:
        dest = run(
            Path(args.dir), daily=args.daily, weekly=args.weekly, rsync_dest=args.rsync_dest
        )
    except Exception as exc:  # noqa: BLE001 — a cron line wants a clear message + nonzero exit
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup ok → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
