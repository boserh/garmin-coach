"""NF-20 forward load forecast: session weights, the pure forecast math (mixed
fact+plan week, empty plan, calibration gate, ACWR thresholds)."""
from app import loadforecast

# --- session weight / load -----------------------------------------------------

def test_type_weights():
    assert loadforecast.session_weight("easy") == 1.5
    assert loadforecast.session_weight("long") == 2.0
    assert loadforecast.session_weight("tempo") == 3.0
    assert loadforecast.session_weight("intervals") == 4.0
    assert loadforecast.session_weight("strength") == 2.0
    assert loadforecast.session_weight("unknown-type") == loadforecast._DEFAULT_WEIGHT


def test_cycling_weight_uses_hr_zone_from_steps():
    session = {"type": "cycling", "steps": [
        {"kind": "warmup", "dur_s": 600, "hr_zone": 2},
        {"kind": "run", "dur_s": 1200, "hr_zone": 4},
    ]}
    assert loadforecast.session_weight("cycling", loadforecast._session_hr_zone(session)) == 4.0


def test_cycling_weight_falls_back_without_hr_zone():
    assert loadforecast.session_weight("cycling", None) == loadforecast._DEFAULT_WEIGHT


def test_session_load_dist_km_estimate():
    # 10 km at anchor pace 5.0 min/km → 50 min x easy weight 1.5 = 75.
    session = {"type": "easy", "dist_km": 10}
    assert loadforecast.session_load(session, anchor_pace=5.0) == 75.0


def test_session_load_zero_without_duration_signal():
    assert loadforecast.session_load({"type": "rest"}) == 0.0


# --- forecast_week ---------------------------------------------------------------

def test_calibrating_below_min_history():
    out = loadforecast.forecast_week(
        remaining_sessions=[], done_load=100.0, chronic_weekly_loads=[80, 90, 100, 110],
        history_days=10,
    )
    assert out["calibrating"] is True
    assert out["load"] == 100.0
    assert "acwr" not in out


def test_calibrating_without_chronic_weeks():
    out = loadforecast.forecast_week(
        remaining_sessions=[], done_load=50.0, chronic_weekly_loads=[],
        history_days=60,
    )
    assert out["calibrating"] is True


def test_mixed_fact_and_plan_week_computes_acwr():
    remaining = [
        {"type": "tempo", "dist_km": 10},   # 50 min @ pace 5.0 x 3.0 = 150
        {"type": "easy", "dist_km": 5},     # 25 min x 1.5 = 37.5
    ]
    out = loadforecast.forecast_week(
        remaining_sessions=remaining, done_load=100.0,
        chronic_weekly_loads=[100.0, 100.0, 100.0, 100.0],
        history_days=40, anchor_pace=5.0,
    )
    assert out["calibrating"] is False
    assert out["load"] == 287.5
    assert out["typical"] == 100.0
    assert out["delta_pct"] == 188
    assert out["acwr"] == 2.88
    assert out["level"] == "high"


def test_acwr_thresholds_ok_warn_high():
    def lvl(total):
        return loadforecast.forecast_week(
            remaining_sessions=[], done_load=total, chronic_weekly_loads=[100.0] * 4,
            history_days=40,
        )["level"]

    assert lvl(100.0) == "ok"
    assert lvl(139.0) == "ok"
    assert lvl(140.0) == "warn"
    assert lvl(159.0) == "warn"
    assert lvl(160.0) == "high"


def test_skipped_session_excluded_by_caller_lowers_forecast():
    # A caller only ever passes "planned"-status sessions — a skip drops out at the
    # call site, so removing a session from remaining_sessions must lower the total.
    with_session = loadforecast.forecast_week(
        remaining_sessions=[{"type": "long", "dist_km": 20}], done_load=0.0,
        chronic_weekly_loads=[100.0] * 4, history_days=40, anchor_pace=5.0,
    )
    without_session = loadforecast.forecast_week(
        remaining_sessions=[], done_load=0.0,
        chronic_weekly_loads=[100.0] * 4, history_days=40, anchor_pace=5.0,
    )
    assert without_session["load"] < with_session["load"]


def test_empty_plan_week_is_just_done_load():
    out = loadforecast.forecast_week(
        remaining_sessions=[], done_load=42.0, chronic_weekly_loads=[100.0] * 4,
        history_days=40,
    )
    assert out["load"] == 42.0
    assert out["calibrating"] is False


def test_week_end_is_the_iso_sunday():
    import datetime as dt

    monday = dt.date(2026, 7, 27)  # a Monday
    assert loadforecast.week_end(monday) == dt.date(2026, 8, 2)
    sunday = dt.date(2026, 8, 2)
    assert loadforecast.week_end(sunday) == sunday
