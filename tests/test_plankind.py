"""app.plankind: a planned session's ``type`` decides which columns it may own."""
from types import SimpleNamespace

from app import plankind


def _row(**kw):
    base = dict(type=None, dist_km=None, steps=None, garmin_template_id=None,
                strength_plan=None, strength_snapshot=None, exercise_edits=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_is_strength():
    assert plankind.is_strength("strength") and plankind.is_strength("STRENGTH")
    assert not plankind.is_strength("easy")
    assert not plankind.is_strength(None)


def test_a_consistent_row_has_nothing_foreign():
    run = _row(type="easy", dist_km=5.0, steps=[{"kind": "run", "dist_m": 5000}])
    strength = _row(type="strength", garmin_template_id=931013083,
                    strength_snapshot={"exercises": []})
    assert plankind.foreign_columns(run) == []
    assert plankind.foreign_columns(strength) == []


def test_a_typeless_row_is_left_alone():
    """Nothing to judge it against — guessing would delete real data."""
    w = _row(type=None, dist_km=5.0, garmin_template_id=1)
    assert plankind.foreign_columns(w) == []
    assert plankind.reconcile(w) == []
    assert w.dist_km == 5.0 and w.garmin_template_id == 1


def test_reconcile_strips_strength_content_from_a_run():
    w = _row(type="easy", dist_km=3.0, steps=[{"kind": "run", "dist_m": 3000}],
             garmin_template_id=931013083, strength_snapshot={"exercises": []},
             exercise_edits=[{"from": "PLANK", "to": "SQUAT"}])
    assert plankind.reconcile(w) == ["garmin_template_id", "strength_snapshot",
                                     "exercise_edits"]
    assert w.garmin_template_id is None and w.exercise_edits is None
    assert w.dist_km == 3.0 and w.steps                      # the run itself survives


def test_reconcile_strips_a_distance_from_a_strength_day():
    w = _row(type="strength", garmin_template_id=931013083, dist_km=4.5,
             steps=[{"kind": "run", "dist_m": 4500}])
    assert plankind.reconcile(w) == ["dist_km", "steps"]
    assert w.dist_km is None and w.steps is None
    assert w.garmin_template_id == 931013083


def test_reconcile_is_idempotent():
    w = _row(type="easy", dist_km=3.0, strength_plan={"blocks": []})
    assert plankind.reconcile(w) == ["strength_plan"]
    assert plankind.reconcile(w) == []
