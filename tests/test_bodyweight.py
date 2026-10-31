"""NF-26 — body weight: a smoothed trend, a robust rate, and two flags.

Sensitive by nature, so half of these tests are about what must NOT happen: no tile and no
error without weigh-ins, no flag from one salty dinner, no raw weight in front of the
analyst, and no normative wording anywhere the analyst is told about it."""
import datetime as dt
from types import SimpleNamespace

import pytest

from app import bodyweight
from app.analysis.cache import _as_dict, _cache_key
from app.analysis.client import CallStats

TODAY = dt.date(2026, 10, 28)


def _hist(weights_by_offset):
    """{days_ago: kg} → read_history-shaped rows (weight inside extra, as stored)."""
    return [{"date": (TODAY - dt.timedelta(days=d)).isoformat(), "extra": {"weight_kg": kg}}
            for d, kg in sorted(weights_by_offset.items(), reverse=True)]


def _steady(kg=70.0, pct_per_week=0.0, every=3, days=42):
    out = {}
    for d in range(0, days, every):
        out[d] = round(kg * (1 + pct_per_week / 100 * d / 7.0), 2)  # losing → heavier back then
    return out


# ---------- the pure module ----------

def test_no_weigh_ins_no_trend():
    assert bodyweight.trend([], today=TODAY) is None
    assert bodyweight.trend([{"date": TODAY.isoformat(), "extra": {"hrv": 50}}],
                            today=TODAY) is None
    assert bodyweight.to_context(None) is None


def test_sparse_weigh_ins_build_a_trend_but_rate_needs_enough_points():
    # two weigh-ins a week for four weeks → a trend AND a rate
    t = bodyweight.trend(_hist(_steady(every=3, days=28)), today=TODAY)
    assert t is not None and t["rate_kg_week"] is not None
    # three weigh-ins → a trend line, but no honest rate yet
    t = bodyweight.trend(_hist({0: 70.0, 9: 70.4, 20: 70.8}), today=TODAY)
    assert t is not None and t["rate_kg_week"] is None and t["flags"] == []


def test_ema_damps_a_single_reading():
    pts = [(TODAY - dt.timedelta(days=d), 70.0) for d in (12, 9, 6, 3)] + [(TODAY, 72.0)]
    smooth = bodyweight.ema(pts)
    assert smooth[-1] < 70.6          # +2 kg moves the line by a fraction, not 2 kg


@pytest.mark.parametrize("outlier", [+2.0, -2.0])
def test_one_outlier_does_not_flip_a_flag(outlier):
    weights = _steady(pct_per_week=0.3, every=3, days=28)
    t_clean = bodyweight.trend(_hist(weights), today=TODAY, volume_delta_pct=25)
    weights[6] = weights[6] + outlier            # one salty dinner (or a dehydrated morning)
    t = bodyweight.trend(_hist(weights), today=TODAY, volume_delta_pct=25)
    assert t_clean["flags"] == [] and t["flags"] == []


def test_fast_loss_flags():
    t = bodyweight.trend(_hist(_steady(pct_per_week=1.4)), today=TODAY)
    assert "fast_loss" in t["flags"] and t["rate_pct_week"] < -1.0


def test_loss_on_rising_volume_needs_the_volume():
    w = _hist(_steady(pct_per_week=0.7))
    assert bodyweight.trend(w, today=TODAY, volume_delta_pct=None)["flags"] == []
    assert bodyweight.trend(w, today=TODAY, volume_delta_pct=5)["flags"] == []
    assert bodyweight.trend(w, today=TODAY, volume_delta_pct=20)["flags"] == [
        "loss_on_rising_volume"]


def test_gain_never_flags():
    t = bodyweight.trend(_hist(_steady(pct_per_week=-1.5)), today=TODAY, volume_delta_pct=40)
    assert t["flags"] == [] and t["rate_kg_week"] > 0


def test_projection_respects_the_goal_horizon():
    w = _hist(_steady(pct_per_week=0.5))
    near = bodyweight.trend(w, today=TODAY,
                            target_date=(TODAY + dt.timedelta(weeks=6)).isoformat())
    far = bodyweight.trend(w, today=TODAY,
                           target_date=(TODAY + dt.timedelta(weeks=30)).isoformat())
    assert near["projected_kg"] < near["current_kg"]
    assert far["projected_kg"] is None


def test_junk_values_are_ignored():
    rows = _hist({0: 70.0, 3: 70.1, 6: 70.2}) + [
        {"date": TODAY.isoformat(), "extra": {"weight_kg": "?"}},
        {"date": "bad", "extra": {"weight_kg": 70}},
        {"date": (TODAY - dt.timedelta(days=1)).isoformat(), "extra": {"weight_kg": 7000}},
    ]
    assert bodyweight.trend(rows, today=TODAY)["n_points"] == 3


_NORMATIVE = ("ідеал", "зайв", "надмірн", "нормальн", "схудн", "погладш", "товст", "худ")


def test_context_is_descriptive_and_carries_no_absolute_weight():
    """The AC: the analyst's section is descriptive — rates and directions, never a number
    on the scale, never a judgement — and it only exists when a flag fired."""
    quiet = bodyweight.trend(_hist(_steady(pct_per_week=0.2)), today=TODAY)
    assert bodyweight.to_context(quiet) is None

    t = bodyweight.trend(_hist(_steady(pct_per_week=1.4)), today=TODAY, volume_delta_pct=20)
    ctx = bodyweight.to_context(t)
    assert set(ctx) == {"flags", "rate_pct_week", "volume_delta_pct", "detail", "tone"}
    assert ctx["tone"] == bodyweight.TONE_NOTE
    text = " ".join(ctx["detail"]).lower()
    assert not any(w in text for w in _NORMATIVE)
    assert "кг" not in text and str(t["current_kg"]) not in text


def test_the_report_prompt_describes_the_tone_rule():
    from app.analysis.prompts import SYSTEM

    assert "body_weight" in SYSTEM
    assert "НІКОЛИ не називай саму вагу" in SYSTEM


def test_summary_for_goal_is_descriptive():
    t = bodyweight.trend(_hist(_steady(pct_per_week=1.4)), today=TODAY,
                         target_date=(TODAY + dt.timedelta(weeks=4)).isoformat())
    s = bodyweight.summary(t).lower()
    assert "кг/тиж" in s and "якщо темп збережеться" in s
    assert not any(w in s for w in _NORMATIVE)


# ---------- storage / prompt plumbing ----------

def test_body_metrics_converts_grams_and_keeps_only_what_came():
    from app.garmin import service

    assert service.body_metrics({"weight": 72350, "bodyFat": 15.2, "muscleMass": 33100,
                                 "boneMass": None}) == {
        "weight_kg": 72.35, "body_fat_pct": 15.2, "muscle_kg": 33.1}
    assert service.body_metrics({"weight": 71.5}) == {"weight_kg": 71.5}
    assert service.body_metrics({"bodyFat": 15.0}) == {}      # no weight → nothing
    assert service.body_metrics(None) == {}


def test_daily_body_prefers_the_days_average():
    from app.garmin import service

    w = {"totalAverage": {"weight": 70000},
         "dateWeightList": [{"date": 1, "weight": 69000}, {"date": 2, "weight": 71000}]}
    assert service._daily_body(w) == {"weight_kg": 70.0}
    w = {"totalAverage": {"weight": None},
         "dateWeightList": [{"date": 2, "weight": 71000}, {"date": 1, "weight": 69000}]}
    assert service._daily_body(w) == {"weight_kg": 71.0}
    assert service._daily_body({}) == {}


def test_switch_off_means_no_weight_fetch(monkeypatch):
    from app.core.config import settings
    from app.garmin import client, service

    monkeypatch.setattr(settings, "BODY_METRICS", False)
    for name in ("fetch_sleep", "fetch_hrv", "fetch_stress", "fetch_body_battery",
                 "fetch_training_readiness", "fetch_user_summary", "fetch_vo2max",
                 "fetch_endurance"):
        monkeypatch.setattr(client, name, lambda *a, **k: {})
    monkeypatch.setattr(client, "fetch_race_predictions", lambda: {})
    monkeypatch.setattr(client, "fetch_daily_events", lambda d: [])
    monkeypatch.setattr(client, "fetch_training_status", lambda d: {}, raising=False)

    def boom(d):
        raise AssertionError("weight fetched with BODY_METRICS off")

    monkeypatch.setattr(client, "fetch_weight", boom)
    out = service.daily_summary(TODAY)
    assert "weight_kg" not in (out["extra"] or {})


def test_analyst_never_reads_the_raw_weight():
    payload = {"daily": [{"date": "2026-10-28", "extra": {
        "weight_kg": 70.1, "body_fat_pct": 15.0, "bmi": 22.0, "resting_hr": 48}}],
        "recent_activities": [], "planned_runs": []}
    extra = _as_dict(payload)["daily"][0]["extra"]
    assert extra == {"resting_hr": 48}


def test_cache_key_includes_the_flag_but_not_its_absence():
    base = _cache_key({}, "q", "m")
    assert _cache_key({}, "q", "m", body_weight=None) == base
    assert _cache_key({}, "q", "m", body_weight={"flags": ["fast_loss"]}) != base


def test_merge_weigh_ins_touches_only_existing_rows():
    from app.cli import merge_weigh_ins

    rows = {"2026-10-01": SimpleNamespace(extra={"resting_hr": 50}),
            "2026-10-02": SimpleNamespace(extra=None)}
    summaries = [
        {"summaryDate": "2026-10-01", "latestWeight": {"weight": 70100}},
        {"summaryDate": "2026-10-02", "allWeightMetrics": [{"weight": 70000},
                                                           {"weight": 69900}]},
        {"summaryDate": "2026-09-30", "latestWeight": {"weight": 70300}},   # no stored row
        {"summaryDate": "2026-10-03", "latestWeight": {"bodyFat": 15}},     # no weight
        "junk",
    ]
    assert merge_weigh_ins(rows, summaries) == (2, 1)
    assert rows["2026-10-01"].extra == {"resting_hr": 50, "weight_kg": 70.1}
    assert rows["2026-10-02"].extra == {"weight_kg": 69.9}
    assert merge_weigh_ins(rows, summaries) == (0, 1)          # idempotent


def test_correlations_know_the_weight_variable():
    from app import correlations

    assert correlations.name("weight_kg") == "вага"
    assert ("weight_kg", 1, "hrv_avg") in correlations._PAIRS


# ---------- consumers ----------

async def _seed_weights(session, uid, weights):
    from app.db.models import DailyMetric

    today = dt.date.today()
    for d, kg in weights.items():
        session.add(DailyMetric(user_id=uid, date=(today - dt.timedelta(days=d)).isoformat(),
                                hrv_avg=50, extra={"weight_kg": kg}))
    await session.commit()


async def test_daily_report_gets_the_flag_only_when_it_fires(session, monkeypatch):
    from app.analysis import reports

    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None, plan_recent=None,
                     body_weight=None, acclimation=None):
        captured["bw"] = body_weight
        return "звіт", CallStats(kind=kind or "report", model="m")

    monkeypatch.setattr(reports, "analyze_with_stats", fake_analyze)
    await _seed_weights(session, 2601, _steady(pct_per_week=1.5))
    await reports.run_analysis(session, {"daily": [], "recent_activities": [],
                                         "planned_runs": []}, user_id=2601)
    assert captured["bw"]["flags"] == ["fast_loss"]

    await _seed_weights(session, 2602, _steady(pct_per_week=0.1))
    await reports.run_analysis(session, {"daily": [], "recent_activities": [],
                                         "planned_runs": []}, user_id=2602)
    assert captured["bw"] is None


async def test_switch_off_silences_every_consumer(session, monkeypatch):
    from app.analysis.reports import build_body_weight_trend
    from app.core.config import settings

    await _seed_weights(session, 2603, _steady(pct_per_week=1.5))
    assert await build_body_weight_trend(session, user_id=2603) is not None
    monkeypatch.setattr(settings, "BODY_METRICS", False)
    assert await build_body_weight_trend(session, user_id=2603) is None


def test_dashboard_tile_only_with_weigh_ins(client):
    import anyio

    from app.db.base import async_session_maker
    from tests.web_helpers import _seed_user, _user_id

    _seed_user(email="weight-none@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "weight-none@example.com", "password": "pw"})
    r = client.get("/dashboard")
    assert r.status_code == 200 and "Вага (згладжений тренд)" not in r.text

    _seed_user(email="weight-some@example.com", password="pw", is_admin=False)
    uid = _user_id("weight-some@example.com")

    async def seed():
        async with async_session_maker() as s:
            await _seed_weights(s, uid, _steady(pct_per_week=1.5))

    anyio.run(seed)
    client.post("/login", data={"email": "weight-some@example.com", "password": "pw"})
    r = client.get("/dashboard")
    assert "Вага (згладжений тренд)" in r.text and "кг/тиж" in r.text
    assert "Вага знижується швидше" in r.text
