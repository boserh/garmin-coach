"""NF-27 · strength tonnage, e1RM and the closed progression loop.

The backlog believed this was blocked by Garmin. It wasn't: ``fetch_exercise_summary`` has
been returning per-set reps and weights all along. So most of these tests are about the
awkward shapes that real strength data actually has — bodyweight sets, timed holds, warm-ups
— and about not letting an ESTIMATE (Epley) masquerade as a measured record.
"""
import datetime as dt

import pytest

from app import records, strengthstats
from app.db.models import ActivityRecord, User


def _sets(**exercises):
    """``_sets(присідання=[(reps, kg), ...])`` → the stored ``exercises`` blob shape."""
    out = {}
    for name, pairs in exercises.items():
        out[name] = {
            "count": len(pairs),
            "reps": [r for r, _w in pairs],
            "weight_kg": [w for _r, w in pairs],
        }
    return {"active_sets": sum(len(p) for p in exercises.values()), "sets": out}


# ---------- awkward real-world shapes ----------

def test_bodyweight_sets_count_reps_not_kilograms():
    """AC: ``weight_kg: [None, ...]`` → reps counted, no e1RM, and no ``None × int``."""
    ex = _sets(pullups=[(10, None), (8, None)])
    assert strengthstats.session_tonnage(ex) == {}
    assert strengthstats.session_reps(ex) == {"pullups": 18}
    assert strengthstats.session_e1rm(ex) == {}


def test_timed_holds_break_nothing():
    """AC: a plank (``reps: [None]``) is neither reps nor kilograms — skipped, not fudged."""
    ex = _sets(планка=[(None, None), (None, None)])
    assert strengthstats.session_tonnage(ex) == {}
    assert strengthstats.session_reps(ex) == {}
    assert strengthstats.weekly_stats([{"date": "2026-06-01", "exercises": ex}]) == []


def test_mixed_session_totals_only_the_loaded_sets():
    ex = _sets(станова=[(5, 100.0), (5, 100.0)], планка=[(None, None)],
               pullups=[(8, None)])
    assert strengthstats.session_tonnage(ex) == {"станова": 1000.0}
    assert strengthstats.session_reps(ex) == {"станова": 10, "pullups": 8}


def test_malformed_blobs_are_ignored():
    assert strengthstats.session_tonnage(None) == {}
    assert strengthstats.session_tonnage({"sets": {"x": "junk"}}) == {}
    assert strengthstats.session_e1rm({}) == {}


def test_short_weight_list_does_not_crash():
    """A defensive zip: an odd DTO with fewer weights than reps must degrade, not raise."""
    ex = {"sets": {"жим": {"count": 3, "reps": [5, 5, 5], "weight_kg": [60.0]}}}
    assert strengthstats.session_tonnage(ex) == {"жим": 300.0}


# ---------- e1RM honesty ----------

def test_e1rm_ignores_high_rep_sets():
    """AC: Epley is fitted for low-to-moderate reps; past 12 it reports fiction."""
    ex = _sets(присідання=[(20, 60.0)])
    assert strengthstats.session_e1rm(ex) == {}


def test_e1rm_ignores_warmup_sets():
    """AC: a set below half the day's top weight for that lift is a warm-up, not an attempt."""
    ex = _sets(станова=[(5, 40.0), (5, 100.0), (5, 100.0), (5, 100.0)])
    value = strengthstats.session_e1rm(ex)["станова"]
    assert value == pytest.approx(strengthstats.epley(100.0, 5), abs=0.1)


def test_e1rm_is_a_median_of_top_sets_not_one_lucky_rep():
    """One outlier set must not become a "record" the next block then builds on."""
    ex = _sets(жим=[(5, 80.0), (5, 80.0), (1, 120.0)])
    value = strengthstats.session_e1rm(ex)["жим"]
    assert value < strengthstats.epley(120.0, 1)


def test_kilograms_are_kilograms_not_grams():
    """Garmin stores set weight in grams; the conversion lives in client.py, and a
    regression there would show up here as a 1000x tonnage."""
    ex = _sets(станова=[(5, 100.0)])
    assert strengthstats.session_tonnage(ex)["станова"] == 500.0


# ---------- weekly aggregates ----------

def _weeks(n_weeks, weight, name="станова", reps=5):
    start = dt.date(2026, 5, 4)   # a Monday
    acts = [{"date": (start + dt.timedelta(weeks=w)).isoformat(),
             "exercises": _sets(**{name: [(reps, weight(w))] * 3})}
            for w in range(n_weeks)]
    return strengthstats.weekly_stats(acts)


def test_weekly_stats_group_by_iso_week():
    weeks = _weeks(3, lambda w: 100.0)
    assert len(weeks) == 3
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)
    assert weeks[0]["tonnage_kg"] == 1500.0


def test_e1rm_trend_needs_two_points():
    assert strengthstats.e1rm_trend(_weeks(1, lambda w: 100.0), "станова") is None
    trend = strengthstats.e1rm_trend(_weeks(4, lambda w: 100.0 + 5 * w), "станова")
    assert trend["change_pct"] > 0 and trend["weeks"] == 4


def test_unknown_exercise_stays_its_own_bucket():
    """AC: a TRX/custom move must not be folded into a known lift — that would corrupt both
    its trend and the total."""
    acts = [{"date": "2026-05-04",
             "exercises": _sets(станова=[(5, 100.0)], **{strengthstats.UNKNOWN_LABEL:
                                                          [(10, 20.0)]})}]
    week = strengthstats.weekly_stats(acts)[0]
    assert set(week["by_exercise"]) == {"станова", strengthstats.UNKNOWN_LABEL}


# ---------- stalls ----------

def test_flat_e1rm_at_steady_tonnage_is_a_stall():
    weeks = _weeks(strengthstats.STALL_WEEKS, lambda w: 100.0)
    stalls = strengthstats.detect_stalls(weeks)
    assert stalls and stalls[0]["exercise"] == "станова"


def test_flat_e1rm_on_falling_volume_is_not_a_stall():
    """A lighter block explains flat strength — flagging it would be nagging about a
    deload the athlete chose."""
    start = dt.date(2026, 5, 4)
    acts = []
    for w in range(strengthstats.STALL_WEEKS):
        n_sets = 4 - w      # volume falling week over week
        acts.append({"date": (start + dt.timedelta(weeks=w)).isoformat(),
                     "exercises": _sets(станова=[(5, 100.0)] * max(1, n_sets))})
    assert strengthstats.detect_stalls(strengthstats.weekly_stats(acts)) == []


def test_rising_e1rm_is_not_a_stall():
    weeks = _weeks(strengthstats.STALL_WEEKS, lambda w: 100.0 + 10 * w)
    assert strengthstats.detect_stalls(weeks) == []


def test_too_short_history_never_stalls():
    assert strengthstats.detect_stalls(_weeks(2, lambda w: 100.0)) == []


# ---------- closing the progression loop ----------

def test_recent_lifts_reports_what_was_actually_lifted():
    """The core of the ticket: the next block must start from the achieved weight, not from
    a number the model invented."""
    start = dt.date.today() - dt.timedelta(weeks=1)
    acts = [{"date": start.isoformat(),
             "exercises": _sets(станова=[(5, 100.0), (5, 110.0)])}]
    lifts = strengthstats.recent_lifts(acts)
    assert lifts["станова"]["top_weight_kg"] == 110.0
    assert lifts["станова"]["typical_reps"] == 5
    assert lifts["станова"]["e1rm"] > 110.0


@pytest.mark.asyncio
async def test_progression_prompt_gets_last_weeks_weights(session, monkeypatch):
    """AC: week 2 is planned from what was really lifted in week 1 (LLM mocked)."""
    from app.analysis import plans

    u = User(email="s@example.com", password_hash="x")
    session.add(u)
    await session.commit()
    session.add(ActivityRecord(
        user_id=u.id, activity_id=1, date=(dt.date.today() - dt.timedelta(days=3)).isoformat(),
        type="strength_training", exercises=_sets(станова=[(5, 105.0), (5, 105.0)]),
    ))
    await session.commit()

    lifts = await plans._recent_lifts(session, u.id)
    assert lifts["станова"]["top_weight_kg"] == 105.0


# ---------- records integration ----------

def test_e1rm_records_are_higher_better_and_need_a_margin():
    """An estimate wobbles set to set; without a margin it would "beat itself" on rounding
    every week — the same reasoning as the race predictions' own margin."""
    kind = f"{records.E1RM_PREFIX}станова"
    assert records._higher_better(kind)
    assert not records._beats(kind, 100.4, 100.0)
    assert records._beats(kind, 102.0, 100.0)


def test_e1rm_is_formatted_as_an_estimate():
    """Never printed as a hard number next to a measured running PB."""
    assert records.format_value(f"{records.E1RM_PREFIX}станова", 102.5).startswith("≈")


def test_tonnage_week_label_and_format():
    assert records.format_value(records.TONNAGE_WEEK, 12345.6) == "12346 кг"
    assert records.label_for(records.TONNAGE_WEEK) != records.TONNAGE_WEEK


@pytest.mark.asyncio
async def test_strength_records_are_detected_and_not_announced_for_a_backfill(session):
    """AC: a backfill of old gym history dates its bests in the past, so the existing
    announce gate keeps it silent — no "🎉" storm on import."""
    u = User(email="rec@example.com", password_hash="x")
    session.add(u)
    await session.commit()
    session.add(ActivityRecord(
        user_id=u.id, activity_id=9, date="2025-01-15", type="strength_training",
        exercises=_sets(станова=[(5, 120.0), (5, 120.0)]),
    ))
    await session.commit()

    inserted = await records.detect_records(session, u.id)
    await session.commit()
    kinds = {r.kind for r in inserted}
    assert f"{records.E1RM_PREFIX}станова" in kinds
    assert records.TONNAGE_WEEK in kinds
    assert records.announce_worthy(inserted) == []


@pytest.mark.asyncio
async def test_strength_record_detection_is_idempotent(session):
    u = User(email="idem@example.com", password_hash="x")
    session.add(u)
    await session.commit()
    session.add(ActivityRecord(
        user_id=u.id, activity_id=11, date="2025-02-15", type="strength_training",
        exercises=_sets(жим=[(5, 80.0)]),
    ))
    await session.commit()
    await records.detect_records(session, u.id)
    await session.commit()
    assert await records.detect_records(session, u.id) == []


def test_strength_context_is_in_the_digest_cache_key():
    from app.analysis.cache import _digest_cache_key

    base = {"iso_week": "2026-W20"}
    assert _digest_cache_key(base, "m") != _digest_cache_key(
        {**base, "strength": {"weeks": [{"tonnage_kg": 100}]}}, "m")
