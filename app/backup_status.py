"""OPS-08 · backup freshness — pure, zero-LLM read of the marker
``scripts/backup_db.py`` writes after each run. No network, no DB — a stat/read of
one small JSON file, so it's cheap enough to call from every morning tick and every
``/status``/dashboard render.

"Backup without monitoring is a lottery": OPS-02 made the nightly backup + off-SD
rsync happen, but nothing watched that it KEEPS happening — a dead systemd timer, a
disconnected USB stick, a full disk all fail silently until the day of a restore.
This module only reads the marker and applies the warn/guard rules; the marker
itself is written by ``scripts/backup_db.py::_write_marker``.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Optional, TypedDict

MARKER_NAME = "last_ok.json"


class BackupStatus(TypedDict):
    age_hours: Optional[float]
    rsync_ok: Optional[bool]
    rsync_error: Optional[str]


# Keep a marker reason from turning a status page or a DM into a wall of rsync output.
_ERROR_MAX = 200


def read_status(backup_dir: Path, *, now: Optional[float] = None) -> BackupStatus:
    """Read the marker. A missing/corrupt file is an honest "no backup yet"
    (``age_hours=None``) rather than a crash — backups may simply not be set up yet."""
    try:
        data = json.loads((backup_dir / MARKER_NAME).read_text())
        ts = float(data["ts"])
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"age_hours": None, "rsync_ok": None, "rsync_error": None}
    now = time.time() if now is None else now
    age_hours = max(0.0, (now - ts) / 3600)
    rsync_ok = data.get("rsync_ok")
    if not isinstance(rsync_ok, bool):
        rsync_ok = None
    # Markers written before the reason existed simply have no key — absent, never "".
    err = data.get("rsync_error")
    rsync_error = err.strip()[:_ERROR_MAX] if isinstance(err, str) and err.strip() else None
    return {"age_hours": round(age_hours, 1), "rsync_ok": rsync_ok, "rsync_error": rsync_error}


def should_warn(age_hours: Optional[float], last_warned: Optional[str], today: str,
                warn_days: int) -> bool:
    """Whether to send the "stale backup" DM today.

    Two deliberately different cadences (OPS-08's own AC):
    - a KNOWN stale backup (``age_hours`` set, past the threshold) nags once/day — it's
      an active, actionable failure (dead timer, full disk).
    - a MISSING marker (backups never configured/ran, ``age_hours is None``) only
      re-nags every ``warn_days`` days — otherwise a brand-new install that hasn't set
      up backups yet would get paged daily for something that isn't a regression.
    """
    if warn_days <= 0:
        return False
    if age_hours is None:
        if last_warned is None:
            return True
        gap_days = (date.fromisoformat(today) - date.fromisoformat(last_warned)).days
        return gap_days >= warn_days
    if age_hours < warn_days * 24:
        return False
    return last_warned != today


def should_warn_rsync(rsync_ok: Optional[bool], last_warned: Optional[str], today: str,
                      warn_days: int) -> bool:
    """Whether to send the "off-SD copy is broken" DM today.

    A separate signal from :func:`should_warn`, and it has to be: the local backup can
    be an hour old (so the staleness check stays silent, as it should) while the rsync
    that carries it OFF the SD card has been failing for months. That state is exactly
    the one this feature exists to catch — a full set of backups on the card that is
    about to die — and until now nothing said a word about it unless the local backup
    happened to be stale too.

    Cadence is every ``warn_days`` days rather than daily: a degraded backup is real but
    not total loss, and the fix (reseat the stick, remount, replace it) can take longer
    than a day. ``rsync_ok is None`` — no ``--rsync-dest`` configured at all — is not a
    failure and never warns; that is a deployment choice, not a regression.
    """
    if warn_days <= 0 or rsync_ok is not False:
        return False
    if last_warned is None:
        return True
    try:
        gap_days = (date.fromisoformat(today) - date.fromisoformat(last_warned)).days
    except ValueError:
        return True
    return gap_days >= warn_days
