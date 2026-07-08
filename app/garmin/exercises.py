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
from typing import Optional

logger = logging.getLogger("garmin")

_PATH = Path(__file__).resolve().parent / "exercise_catalog.json"
# Garmin's translations (key=value): `<CATEGORY>_<EXERCISE>=Label`,
# `category_type_<CATEGORY>=Label`, `exercise_type_<EXERCISE>=Label`.
_PROPS_PATH = Path(__file__).resolve().parent / "exercise_types.properties"


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


def _load_labels() -> dict:
    labels: dict = {}
    try:
        with open(_PROPS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#!" or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                labels[k.strip()] = v.strip()
    except FileNotFoundError:
        pass  # labels are optional — fall back to prettified codes
    except OSError as e:
        logger.warning(f"exercise translations load failed: {e}")
    return labels


# Garmin's strength/cardio exercise *categories* (top-level codes). Kept as a built-in so
# category validation works even before the full `exercise_catalog.json` is dropped in
# (the catalog then adds the per-category exercise *variants*).
_FALLBACK_CATEGORIES = {
    "BANDED_EXERCISES", "BATTLE_ROPE", "BENCH_PRESS", "BIKE_OUTDOOR", "CALF_RAISE",
    "CARDIO", "CARRY", "CHOP", "CORE", "CRUNCH", "CURL", "DEADLIFT", "ELLIPTICAL",
    "FLOOR_CLIMB", "FLYE", "HIP_RAISE", "HIP_STABILITY", "HIP_SWING", "HYPEREXTENSION",
    "INDOOR_BIKE", "LADDER", "LATERAL_RAISE", "LEG_CURL", "LEG_RAISE", "LUNGE",
    "OLYMPIC_LIFT", "PLANK", "PLYO", "PULL_UP", "PUSH_UP", "ROW", "RUN", "RUN_INDOOR",
    "SANDBAG", "SHOULDER_PRESS", "SHOULDER_STABILITY", "SHRUG", "SIT_UP", "SLED",
    "SLEDGE_HAMMER", "SQUAT", "STAIR_STEPPER", "SUSPENSION", "TIRE", "TOTAL_BODY",
    "TRICEPS_EXTENSION", "WARM_UP",
}

CATALOG = _load()
CATEGORIES = sorted(set(CATALOG) | _FALLBACK_CATEGORIES)
LABELS = _load_labels()


def _exercises(category: str) -> dict:
    return (CATALOG.get((category or "").upper()) or {}).get("exercises", {}) or {}


def valid_category(category: str) -> bool:
    return bool(category) and category.upper() in (set(CATALOG) | _FALLBACK_CATEGORIES)


def valid_exercise(category: str, exercise: str) -> bool:
    """True if ``exercise`` is a real variant of ``category`` (empty ``exercise`` is OK —
    Garmin allows a bare category, like the user's HYPEREXTENSION step)."""
    if not exercise:
        return True
    variants = _exercises(category)
    if not variants:  # catalog absent → can't validate the variant, accept it
        return True
    return exercise.upper() in variants


def exercises_for(category: str) -> list:
    """The valid exerciseName variants for a category (for pickers / prompt context)."""
    return sorted(_exercises(category))


# Invalid exercise names already logged, to keep the log to one line per bad code.
_invalid_seen: set = set()


def check_exercise(category: str, exercise: Optional[str]) -> Optional[str]:
    """Normalise an exercise name for storage: the uppercased code if it's a real variant
    of ``category`` (or if the catalog is absent — can't validate), else ``None`` so the
    step stays category-only (valid for Garmin). Empty ``exercise`` → ``None``. A rejected
    name is logged once as ``EXERCISE invalid: <CAT>/<NAME>``."""
    if not exercise:
        return None
    ex = exercise.upper()
    if valid_exercise(category, ex):
        return ex
    key = f"{(category or '').upper()}/{ex}"
    if key not in _invalid_seen:
        _invalid_seen.add(key)
        logger.info(f"EXERCISE invalid: {key}")
    return None


def prettify(code: str) -> str:
    """A readable label from a Garmin code: 'BARBELL_DEADLIFT' → 'Barbell Deadlift'."""
    return (code or "").strip("_").replace("_", " ").title()


def label(category: str, exercise: str = "") -> str:
    """Human label for a code, via Garmin's translations, else a prettified fallback.
    Prefers the specific `<CATEGORY>_<EXERCISE>` string, then the bare category name."""
    cat, exn = (category or "").upper(), (exercise or "").upper()
    if exn:
        for key in (f"{cat}_{exn}", f"exercise_type_{exn}"):
            if key in LABELS:
                return LABELS[key]
    if f"category_type_{cat}" in LABELS:
        return LABELS[f"category_type_{cat}"]
    return prettify(exercise or category)
