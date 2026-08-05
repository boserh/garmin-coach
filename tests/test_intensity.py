"""NF-24 · intensity distribution: the pure detector, the DTO parse, and the gates.

Most of these assert on SILENCE. The feature's failure mode isn't a wrong number, it's a
confident one: "40% grey zone" computed off two runs would be acted on, and acting on noise
is worse than having no feature.
"""
import datetime as dt

import pytest

from app import intensity
from app.core.config import settings
from app.garmin import client


def _acts(specs, start=dt.date(2026, 6, 1)):
    """``specs`` is a list of (day_offset, low_s, gray_s, high_s[, te_anaer])."""
    out = []
    for spec in specs:
        offset, low, gray, high = spec[:4]
        te = spec[4] if len(spec) > 4 else None
        zones = {"z1_s": low, "z3_s": gray, "z4_s": high}
        if te is not None:
            zones["te_anaer"] = te
        out.append({"date": (start + dt.timedelta(days=offset)).isoformat(),
                    "type": "running", "zones": zones})
    return out


# ---------- degradation ----------

def test_activity_without_zones_is_skipped_not_zeroed():
    """An old activity or one recorded without a HR strap must not drag a week's shares —
    it carries no information about intensity, and counting it as zero would say it did."""
    acts = _acts([(0, 3000, 100, 100), (1, 3000, 100, 100), (2, 3000, 100, 100)])
    acts.append({"date": "2026-06-03", "type": "strength_training", "zones": None})
    acts.append({"date": "2026-06-04", "type": "kitesurfing", "zones": {}})
    weeks = intensity.weekly_distribution(acts)
    assert sum(w["sessions"] for w in weeks) == 3


def test_zones_with_only_training_effect_do_not_create_a_week():
    """A row whose ``zones`` holds just te_aer/te_anaer (the list DTO gave training effect
    but the zone fetch found nothing) has no TIME to distribute — no division by zero, and
    no phantom week."""
    acts = [{"date": "2026-06-01", "type": "running", "zones": {"te_aer": 3.0}}]
    assert intensity.weekly_distribution(acts) == []


def test_thin_week_reports_insufficient_rather_than_shares():
    acts = _acts([(0, 3000, 900, 100), (1, 3000, 900, 100)])   # 2 sessions < MIN
    week = intensity.weekly_distribution(acts)[0]
    assert week["enough"] is False
    assert week["low_frac"] is None and week["gray_frac"] is None


def test_no_findings_from_thin_weeks():
    acts = _acts([(0, 100, 3000, 100), (1, 100, 3000, 100)])   # screaming grey, 2 sessions
    weeks = intensity.weekly_distribution(acts)
    assert intensity.detect(weeks, low_target=0.8, gray_max=0.15, anaerobic_cap=8.0) == []


def test_empty_input_is_silent():
    assert intensity.weekly_distribution([]) == []
    assert intensity.detect([], low_target=0.8, gray_max=0.15, anaerobic_cap=8.0) == []
    assert intensity.summary([], []) is None
    assert intensity.build_context([], []) == {}


def test_unparseable_date_is_dropped():
    acts = [{"date": "not-a-date", "type": "running", "zones": {"z1_s": 100}}]
    assert intensity.weekly_distribution(acts) == []


# ---------- the arithmetic ----------

def test_distribution_is_by_time_not_by_session_count():
    """A 20-minute set of strides must not outvote a 2-hour easy long run."""
    acts = _acts([
        (0, 7200, 0, 0),      # 2h easy
        (1, 0, 0, 600),       # 10 min hard
        (2, 0, 0, 600),       # 10 min hard
    ])
    week = intensity.weekly_distribution(acts)[0]
    assert week["low_frac"] > 0.8, "by session count this would read as 33% easy"


def test_weeks_are_iso_weeks_oldest_first():
    acts = _acts([(0, 600, 0, 0), (1, 600, 0, 0), (2, 600, 0, 0),
                  (7, 600, 0, 0), (8, 600, 0, 0), (9, 600, 0, 0)])
    weeks = intensity.weekly_distribution(acts)
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)
    assert len(weeks) == 2


# ---------- findings ----------

def _gray_weeks(n_weeks=3):
    specs = []
    for w in range(n_weeks):
        for d in range(3):
            specs.append((w * 7 + d, 1000, 900, 100))   # ~45% grey
    return _acts(specs)


def test_gray_zone_needs_a_run_of_weeks():
    """One heavy week is training; three in a row is a pattern. Flagging the first would
    make the coach a nag, and a nag gets muted."""
    one = intensity.weekly_distribution(_gray_weeks(1))
    assert not [f for f in intensity.detect(
        one, low_target=0.8, gray_max=0.15, anaerobic_cap=0) if f["kind"] == "gray_zone"]

    three = intensity.weekly_distribution(_gray_weeks(3))
    assert [f for f in intensity.detect(
        three, low_target=0.8, gray_max=0.15, anaerobic_cap=0) if f["kind"] == "gray_zone"]


def test_planned_intensity_week_suppresses_the_easy_advice():
    """AC: a week the PLAN filled with intensity must not produce "your easy runs are too
    hard" — that would have the coach scolding the athlete for following the coach's plan."""
    weeks = intensity.weekly_distribution(_gray_weeks(3))
    all_weeks = {w["week"] for w in weeks}
    quiet = intensity.detect(weeks, low_target=0.8, gray_max=0.15, anaerobic_cap=0,
                             planned_intensity_weeks=all_weeks)
    assert not [f for f in quiet if f["kind"] in {"gray_zone", "low_share"}]


def test_anaerobic_cap_fires_even_in_a_planned_week():
    """The dose ceiling is about what the body absorbs, so unlike the pacing advice it is
    NOT excused by the plan having prescribed the sessions."""
    acts = _acts([(0, 1000, 100, 500, 4.0), (1, 1000, 100, 500, 4.0),
                  (2, 1000, 100, 500, 4.0)])
    weeks = intensity.weekly_distribution(acts)
    findings = intensity.detect(
        weeks, low_target=0.0, gray_max=1.0, anaerobic_cap=8.0,
        planned_intensity_weeks={w["week"] for w in weeks})
    assert [f for f in findings if f["kind"] == "anaerobic_over"]


def test_anaerobic_cap_of_zero_disables_that_check():
    acts = _acts([(0, 100, 0, 500, 9.0), (1, 100, 0, 500, 9.0), (2, 100, 0, 500, 9.0)])
    weeks = intensity.weekly_distribution(acts)
    findings = intensity.detect(weeks, low_target=0.0, gray_max=1.0, anaerobic_cap=0)
    assert not [f for f in findings if f["kind"] == "anaerobic_over"]


def test_low_share_fires_below_target():
    acts = _acts([(0, 500, 200, 500), (1, 500, 200, 500), (2, 500, 200, 500)])
    weeks = intensity.weekly_distribution(acts)
    findings = intensity.detect(weeks, low_target=0.8, gray_max=1.0, anaerobic_cap=0)
    assert [f for f in findings if f["kind"] == "low_share"]


def test_summary_and_context_shapes():
    acts = _acts([(0, 3000, 100, 100), (1, 3000, 100, 100), (2, 3000, 100, 100)])
    weeks = intensity.weekly_distribution(acts)
    findings = intensity.detect(weeks, low_target=0.8, gray_max=0.15, anaerobic_cap=8.0)
    assert "Інтенсивність" in intensity.summary(weeks, findings)
    ctx = intensity.build_context(weeks, findings)
    assert ctx["weeks"][-1]["low_pct"] + ctx["weeks"][-1]["gray_pct"] + \
        ctx["weeks"][-1]["high_pct"] == pytest.approx(100, abs=1)


# ---------- Garmin DTO parse ----------

def test_zone_dto_parse(monkeypatch):
    monkeypatch.setattr(client, "_cache_get", lambda k: None)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)
    monkeypatch.setattr(client, "_safe", lambda fn, *a, **kw: [
        {"zoneNumber": 1, "secsInZone": 600.0},
        {"zoneNumber": 2, "secsInZone": 1200.7},
        {"zoneNumber": 3, "secsInZone": 0.0},          # empty zone → omitted
        {"zoneNumber": 9, "secsInZone": 100},          # out of range → ignored
        {"zoneNumber": None, "secsInZone": 50},        # malformed → ignored
        "junk",                                         # not a dict → ignored
    ])
    assert client.fetch_activity_zones(123) == {"z1_s": 600, "z2_s": 1201}


def test_zone_dto_error_is_not_cached(monkeypatch):
    """A transient failure must not be stored as "this activity has no zones" for a year."""
    puts = []
    monkeypatch.setattr(client, "_cache_get", lambda k: None)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: puts.append(k))
    monkeypatch.setattr(client, "_safe", lambda fn, *a, **kw: {"_error": "boom"})
    assert client.fetch_activity_zones(123) == {}
    assert not puts


def test_hr_zone_thresholds_parse(monkeypatch):
    monkeypatch.setattr(client, "_cache_get", lambda k: None)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)
    monkeypatch.setattr(client, "_safe", lambda fn, *a, **kw: [
        {"zone1Floor": 100.0, "zone2Floor": 120, "zone5Floor": 170,
         "maxHeartRateUsed": 190},
    ])
    z = client.fetch_hr_zones()
    assert z["z1"] == 100 and z["z2"] == 120 and z["z5"] == 170 and z["max_hr"] == 190


def test_hr_zones_tolerate_an_unexpected_shape(monkeypatch):
    """The NF-15 lesson: a desk-verified endpoint shape can be wrong, so an unfamiliar DTO
    degrades to "no zone context", never to a crash on the morning path."""
    monkeypatch.setattr(client, "_cache_get", lambda k: None)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)
    monkeypatch.setattr(client, "_safe", lambda fn, *a, **kw: {"_error": "403"})
    assert client.fetch_hr_zones() == {}


# ---------- injury radar coupling ----------

def test_gray_zone_raises_injury_score_but_never_warns_alone():
    """Contributing pattern, not a warning of its own — weighted below repeated pain on
    purpose."""
    from app import injury

    findings = [{"kind": "gray_zone", "gray_frac": 0.42, "weeks": 3}]
    quiet = injury.assess([], [], history_days=90)
    loud = injury.assess([], [], history_days=90, intensity_findings=findings)
    assert loud.score > quiet.score
    assert loud.level not in {"high"}


# ---------- settings toggle ----------

@pytest.mark.asyncio
async def test_disabled_toggle_returns_empty_context(session, monkeypatch):
    from app.analysis.reports import build_intensity_context

    monkeypatch.setattr(settings, "INTENSITY_DISTRIBUTION", False)
    assert await build_intensity_context(session, user_id=1) == {}


def test_intensity_is_in_the_daily_cache_key():
    """The backlog's cross-cutting trap: context outside the key means a week that just
    drifted into the grey zone keeps returning yesterday's report."""
    from app.analysis.cache import _cache_key

    base = dict(data={}, question="q", model="m")
    a = _cache_key(**base)
    b = _cache_key(**base, intensity={"findings": [{"kind": "gray_zone"}]})
    assert a != b
