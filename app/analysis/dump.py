"""Opt-in on-disk copy of every Claude request, written as it goes out (PROMPT_DUMP_DIR).

Why this exists: the request body is not persisted anywhere. ``report_logs`` records the
question string, the token counts and the delivered answer — but never the ``user_content``
that was posted. So when a report describes a day wrongly there is no way, after the fact,
to tell an incomplete context from a bad narration: the evidence was never kept. (This is
exactly how a morning report came to narrate auto-detected rides from the wrong day while
missing a two-hour session: reconstructing the request was impossible, because
``recent_activities`` is a live Garmin fetch that leaves no trace of what it returned.)

With ``PROMPT_DUMP_DIR`` set, each call drops one JSON file — model, system prompt and the
full user content — next to a timestamp, so the request can be read back verbatim.

Deliberately best-effort: a failed dump logs and returns, never breaking the call it was
observing. Off by default, and the files hold the athlete's data in the clear, so treat
the directory like the database.
"""
import datetime as dt
import json
import logging
import os
from typing import Optional

from app.core.config import settings

logger = logging.getLogger("claude")


def _prune(directory: str, keep: int) -> None:
    """Keep the newest ``keep`` dumps. Unbounded growth is the reason a debug switch like
    this gets left on and then fills the SD card."""
    if keep <= 0:
        return
    files = sorted(
        (os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".json")),
        key=os.path.getmtime, reverse=True,
    )
    for path in files[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


def dump_request(*, kind: str, model: str, system: str, user_content: dict,
                 user_id: Optional[int] = None, max_tokens: Optional[int] = None
                 ) -> Optional[str]:
    """Write one outgoing request to ``PROMPT_DUMP_DIR``; return the path, or None when
    the switch is off (or the write failed — never raises)."""
    directory = (settings.PROMPT_DUMP_DIR or "").strip()
    if not directory:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = os.path.join(directory, f"{stamp}-{kind}-u{user_id or 0}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "written_at": dt.datetime.now().isoformat(timespec="seconds"),
                "kind": kind,
                "model": model,
                "max_tokens": max_tokens,
                "user_id": user_id,
                "system": system,
                "user_content": user_content,
            }, fh, ensure_ascii=False, indent=2)
        _prune(directory, settings.PROMPT_DUMP_KEEP)
        logger.info(f"PROMPT DUMP {kind} → {path}")
        return path
    except Exception:  # noqa: BLE001 — observation only, never break the call it observes
        logger.exception("PROMPT DUMP failed")
        return None
