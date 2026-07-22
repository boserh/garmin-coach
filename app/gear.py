"""NF-15 · Shoe mileage tracker.

Worn-out shoes are a banal, frequent injury factor, and our injury radar knows nothing
about footwear. Garmin tracks gear (shoes/other equipment) and its own lifetime mileage
per item — this module is the pure-Python glue: parse the two gear-service responses
defensively, decide which pairs are past the wear threshold, and format the ``/gear``
reply. Zero LLM, zero DB access here — the roster + mileage snapshot is fetched and
persisted by ``bot.jobs._gear_check_for_user`` (a Garmin-fetch concern, like every other
client.py orchestration) into a ``bot_state`` JSON blob (:data:`STATE_KEY`), so ``/gear``
itself is a plain, fast DB read afterwards rather than a live fetch on every tap.

**Recon note (this ticket's own AC #1 — a blocker, not a detail):** neither endpoint's
exact response shape has been verified against a live Garmin account in this codebase.
``app.garmin.client``'s GEAR section documents what's known from the community
``python-garminconnect`` library (``get_gear``/``get_gear_stats``, no activity→gear link
method at all). Every parse function here is defensive — an unrecognised shape is logged
once (``GEAR ... shape unrecognised``) and treated as "no data" rather than guessed at, so
a wrong field name can never produce a false wear warning; it can only under-report.
Deliberately deviates from the ticket's original "sum our own ActivityRecord rows per
gear_id" design for the same reason: no confirmed activity→gear link endpoint exists, so
mileage comes straight from Garmin's own per-gear stats total (already shown as a shoe's
lifetime distance in the Connect UI) — no migration column, no backfill CLI, no risk of
undercounting before a backfill runs.
"""
import logging
from typing import List, Optional

logger = logging.getLogger("gear")

# bot_state keys shared between the daily sync (bot/jobs.py) and the /gear command
# (bot/handlers.py) — a single source of truth so they can never drift apart.
STATE_KEY = "gear_roster"          # JSON list of pairs, refreshed once/day
WARN_PREFIX = "gear_warned:"       # + gear_id -> mileage_km (str) at the last warning

_unmapped: set = set()


def _warn_once(tag: str, shape) -> None:
    if tag in _unmapped:
        return
    _unmapped.add(tag)
    keys = sorted(shape) if isinstance(shape, dict) else type(shape).__name__
    logger.warning(f"GEAR {tag} shape unrecognised: {keys}")


def parse_item(raw: dict) -> Optional[dict]:
    """One ``filterGear`` row -> ``{gear_id, name, type, retired}``, or None for a row we
    can't identify (never guesses at a missing id)."""
    if not isinstance(raw, dict):
        return None
    gid = raw.get("uuid") or raw.get("gearPk") or raw.get("gearUUID") or raw.get("gearId")
    if gid is None:
        _warn_once("item", raw)
        return None
    name = (raw.get("displayName") or raw.get("customMakeModel")
            or raw.get("gearMakeName") or "Спорядження")
    gtype = raw.get("gearTypeName") or raw.get("typeName") or ""
    retired = bool(raw.get("retired")) or (raw.get("gearStatusName") or "").lower() == "retired"
    return {"gear_id": str(gid), "name": str(name), "type": str(gtype), "retired": retired}


def parse_mileage_km(stats: dict) -> Optional[float]:
    """A gear-stats dict's lifetime distance in km, or None when nothing recognisable is
    there. Garmin's distance fields are metres elsewhere in this app (``service.py``'s
    ``totalDistanceMeters``) — the same convention is assumed here."""
    if not isinstance(stats, dict):
        return None
    for key in ("totalDistance", "totalDistanceInMeters", "distance"):
        v = stats.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return round(v / 1000.0, 1)
    if stats:
        _warn_once("stats", stats)
    return None


def parse_last_used(stats: dict) -> Optional[str]:
    """A gear-stats dict's last-used date (ISO, first 10 chars), or None."""
    if not isinstance(stats, dict):
        return None
    for key in ("lastActivityDate", "lastUsedDate", "endDate"):
        v = stats.get(key)
        if isinstance(v, str) and v:
            return v[:10]
    return None


def worn(pairs: List[dict], threshold_km: float) -> List[dict]:
    """Pairs at/over ``threshold_km`` — empty when the threshold is disabled (<=0) or no
    pair qualifies. Retired gear never nags (already benched)."""
    if not threshold_km or threshold_km <= 0:
        return []
    return [p for p in pairs
            if not p.get("retired") and (p.get("mileage_km") or 0) >= threshold_km]


def should_rewarn(mileage_km: Optional[float], last_warned_km: Optional[float],
                   step_km: float) -> bool:
    """True when a pair either hasn't been warned about yet (``last_warned_km`` is None) or
    has piled on another ``step_km`` since its last warning — so a pair left in rotation
    past the threshold gets an occasional nudge, not silence forever after the first DM."""
    if mileage_km is None:
        return False
    if last_warned_km is None:
        return True
    if not step_km or step_km <= 0:
        return False
    return mileage_km >= last_warned_km + step_km


def dominance_note(pairs: List[dict]) -> Optional[str]:
    """NF-15's honesty pitfall: if only one pair carries any real mileage, gear probably
    isn't actually tracked per-shoe in Garmin Connect (everything defaults to one item),
    so the numbers are fiction, not a real fleet — ``/gear`` should say so plainly. None
    when there's nothing to flag."""
    tracked = [p for p in pairs if (p.get("mileage_km") or 0) > 0]
    if len(tracked) != 1 or len(pairs) < 2:
        return None
    return ("Схоже, у Garmin Connect по-справжньому ведеться лише одна пара — якщо це "
            "не так, прив'яжи решту взуття до пробіжок у Garmin Connect для точнішого обліку.")


def summary_line(pair: dict) -> str:
    km = pair.get("mileage_km")
    km_s = f"{km:.0f} км" if isinstance(km, (int, float)) else "невідомо"
    used = pair.get("last_used")
    tail = f", востаннє {used}" if used else ""
    retired = " (у відставці)" if pair.get("retired") else ""
    return f"👟 {pair.get('name') or 'Спорядження'} — {km_s}{tail}{retired}"


def warn_text(pair: dict) -> str:
    km = pair.get("mileage_km") or 0
    return f"👟 {pair.get('name') or 'Взуття'} набігала ~{km:.0f} км — подумай про заміну."
