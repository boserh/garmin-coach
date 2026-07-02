"""Garmin strength-exercise catalog — the authoritative ``category → exerciseName``
taxonomy exported from Garmin Connect (``exercise_catalog.json``). We use it to *validate*
and *resolve* exercises when editing a strength day, so an LLM edit like «станова тяга»
becomes a real Garmin ``category`` (``DEADLIFT``) instead of a hallucinated code — a step
whose ``category``/``exerciseName`` aren't in the catalog is rejected.

Only the codes matter here (the file also carries muscle data we ignore). If the file is
missing, everything degrades gracefully to "unknown" (no validation, no resolution)."""
import json
import logging
from pathlib import Path

logger = logging.getLogger("garmin")

_PATH = Path(__file__).resolve().parent / "exercise_catalog.json"


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("exercise_catalog.json not found — strength exercise editing disabled")
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"exercise catalog load failed: {e}")
        return {}


CATALOG = _load()
CATEGORIES = sorted(CATALOG)


def _exercises(category: str) -> dict:
    return (CATALOG.get((category or "").upper()) or {}).get("exercises", {}) or {}


def valid_category(category: str) -> bool:
    return bool(category) and category.upper() in CATALOG


def valid_exercise(category: str, exercise: str) -> bool:
    """True if ``exercise`` is a real variant of ``category`` (empty ``exercise`` is OK —
    Garmin allows a bare category, like the user's HYPEREXTENSION step)."""
    if not exercise:
        return True
    return exercise.upper() in _exercises(category)


def exercises_for(category: str) -> list:
    """The valid exerciseName variants for a category (for pickers / prompt context)."""
    return sorted(_exercises(category))


def prettify(code: str) -> str:
    """A readable label from a Garmin code: 'BARBELL_DEADLIFT' → 'Barbell Deadlift'."""
    return (code or "").strip("_").replace("_", " ").title()
