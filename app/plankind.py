"""Which columns a planned session's ``type`` is allowed to own.

A ``PlannedWorkout`` is EITHER a **strength** session (a saved Garmin workout to clone via
``garmin_template_id``, or a from-scratch ``strength_plan``) OR a **distance** session
(``dist_km`` + ``steps``). The two sets of columns are mutually exclusive, and ``type`` is
the only thing that says which — the push path reads the *columns*, so a row that carries
both is a session that goes to the watch as the wrong sport entirely.

That is not hypothetical. A plan edit may rewrite ``type`` (``SYSTEM_PLAN_EDIT`` says so in
as many words, and "swap Wednesday's run with Thursday's strength" produces exactly a pair
of ``modify`` ops that do), and the write path used to leave the other set behind — so both
halves of a swap broke at once:

* the run day kept its ``garmin_template_id`` → push cloned the strength template and named
  it ``🏋️ <the run's description>``: the athlete opened "дуже легкий відновлювальний біг
  3 км" on the watch and found hanging leg raises and a plank;
* the strength day kept the run's ``dist_km``/``steps`` → ``/plan`` rendered "СИЛОВА · 4.5 км".

Pure: no DB, no network. Rows are duck-typed (anything with the attributes).
"""
from typing import List, Optional

# Columns only a strength session may carry, and only a distance session may carry.
_STRENGTH_ONLY = ("garmin_template_id", "strength_plan", "strength_snapshot",
                  "exercise_edits")
_DISTANCE_ONLY = ("dist_km", "steps")


def is_strength(type_: Optional[str]) -> bool:
    """True for the one type whose session is built from exercises, not from distance."""
    return (type_ or "").lower() == "strength"


def foreign_columns(w) -> List[str]:
    """The columns set on ``w`` that its ``type`` cannot own — the names, in a stable order,
    of everything that has to be nulled for the row to describe one session again.

    Empty for a consistent row, and empty for a row with no type at all: there is nothing to
    judge it against, and guessing would delete real data.
    """
    if not (getattr(w, "type", None) or "").strip():
        return []
    stale = _DISTANCE_ONLY if is_strength(w.type) else _STRENGTH_ONLY
    return [f for f in stale if getattr(w, f, None) is not None]


def reconcile(w) -> List[str]:
    """Null whatever ``w.type`` cannot own; returns the cleared column names.

    The type is the intent — it is what the athlete asked for and what ``/plan`` renders —
    so it wins over leftovers from what the day used to be.
    """
    cleared = foreign_columns(w)
    for f in cleared:
        setattr(w, f, None)
    return cleared
