"""NF-25: running dynamics — session drift, weekly trend, the injury signal.

Pure module, so these are plain unit tests: no DB, no network, no Claude (the suite spends
$0 by construction here).
"""
from app import injury, rundynamics


def _series(n=60, *, cad=None, gct=None, vo=None, elev=None, pace=6.0, step_km=0.1):
    """A synthetic run: ``n`` points, each channel either a constant or a per-index list."""
    def at(v, i):
        if v is None:
            return None
        return v[i] if isinstance(v, list) else v

    return [
        {"d": round(i * step_km, 2), "p": at(pace, i), "hr": 140,
         "e": at(elev, i), "cad": at(cad, i), "gct": at(gct, i), "vo": at(vo, i)}
        for i in range(n)
    ]


# ---------- the common case: a watch with no running-dynamics accessory ----------

def test_no_dynamics_channels_returns_none():
    """The MAJORITY of setups report no cadence/GCT/oscillation at all. That is not an
    error state — every consumer must simply stay silent."""
    assert rundynamics.session_dynamics(_series(), dur_min=60) is None
    assert rundynamics.session_dynamics(None, dur_min=60) is None
    assert rundynamics.session_dynamics([], dur_min=60) is None
    assert rundynamics.summary(None) is None


def test_averages_and_stride_without_drift_on_a_short_session():
    """Under the 30-minute gate the averages are still reported — only the drift (which
    needs a tired last third to mean anything) is withheld."""
    dyn = rundynamics.session_dynamics(
        _series(cad=180, gct=240, vo=8.0, pace=5.0), dur_min=20)
    assert dyn["avg_cadence"] == 180
    assert dyn["avg_gct_ms"] == 240
    assert dyn["avg_vo_cm"] == 8.0
    # stride = speed (m/min) / cadence = (1000/5.0) / 180
    assert dyn["stride_m"] == round((1000.0 / 5.0) / 180, 2)
    assert "cadence_drift_pct" not in dyn
    assert "drift" not in dyn


# ---------- within-session drift ----------

def test_cadence_falling_away_is_a_drift():
    cad = [182] * 30 + [170] * 30          # form collapses in the back half
    dyn = rundynamics.session_dynamics(_series(cad=cad, elev=100.0), dur_min=60)
    assert dyn["cadence_drift_pct"] < -rundynamics.CADENCE_DRIFT_PCT
    assert dyn["drift"] is True


def test_steady_cadence_is_not_a_drift():
    dyn = rundynamics.session_dynamics(_series(cad=180, elev=100.0), dur_min=60)
    assert dyn["drift"] is False
    assert abs(dyn["cadence_drift_pct"]) < rundynamics.CADENCE_DRIFT_PCT


def test_growing_ground_contact_alone_is_a_drift():
    gct = [235] * 30 + [255] * 30
    dyn = rundynamics.session_dynamics(_series(gct=gct, elev=100.0), dur_min=60)
    assert dyn["gct_drift_pct"] >= rundynamics.GCT_DRIFT_PCT
    assert dyn["drift"] is True


def test_drift_is_measured_on_flat_ground_only():
    """The ticket's own gate, on the ticket's own synthetic profile ("flat → climb").

    The route is flat for the first half and climbs steeply through the second, where the
    cadence legitimately shortens. Reading that as "form drift" would make the number
    useless on any hilly route — so the climb's points must be excluded and the session must
    come out clean."""
    n = 60
    elev = [100.0] * 30 + [100.0 + (i - 29) * 12.0 for i in range(30, n)]  # ~12 m per 100 m
    cad = [180] * 30 + [168] * 30      # cadence drops exactly where the hill starts
    dyn = rundynamics.session_dynamics(
        _series(n=n, cad=cad, elev=elev), dur_min=60)
    assert dyn["flat_filtered"] is True
    assert dyn["drift"] is False, "the climb's cadence drop must not read as form drift"


def test_flat_filter_reports_itself_when_there_is_no_elevation():
    """No altitude channel → we cannot exclude climbs. Better to say so than to pretend the
    route was flat (or to drop a perfectly usable run)."""
    cad = [182] * 30 + [170] * 30
    dyn = rundynamics.session_dynamics(_series(cad=cad, elev=None), dur_min=60)
    assert dyn["flat_filtered"] is False
    assert dyn["drift"] is True
    assert "профіль траси невідомий" in rundynamics.summary(dyn)


# ---------- weekly trend ----------

def _run(date, hr, cad, dur=45):
    return {"date": date, "dur_min": dur, "dist_km": 9.0, "avg_hr": hr,
            "series": _series(cad=cad, elev=100.0)}


def test_weekly_trend_excludes_hard_runs():
    """Cadence on 5×1000 is structurally higher than on a recovery jog, so a trend over
    mixed intensities would describe the training plan, not the runner."""
    easy = [_run(f"2026-{m:02d}-{d:02d}", 135, 178)
            for m, d in ((3, 2), (3, 9), (4, 6), (4, 13), (5, 4), (5, 11))]
    hard = [_run("2026-05-12", 175, 190), _run("2026-05-13", 178, 191)]
    trend = rundynamics.build_trend(easy + hard)
    assert trend["status"] == "ok"
    # Every weekly median comes from the easy runs only — a 190 would show immediately.
    assert all(w["cadence"] == 178 for w in trend["weekly"])


def test_weekly_trend_calibrates_before_it_speaks():
    """Two weeks of data is not a trend, and fitting a slope to it would be noise dressed
    up as progress — the same honesty gate NF-19's efficiency trend uses."""
    trend = rundynamics.build_trend([_run("2026-05-04", 135, 178),
                                     _run("2026-05-05", 135, 178),
                                     _run("2026-05-11", 135, 179)])
    assert trend["status"] == "calibrating"
    assert "cadence_slope_per_week" not in trend


def test_weekly_trend_is_none_without_dynamics():
    runs = [{"date": "2026-05-04", "dur_min": 45, "dist_km": 9.0, "avg_hr": 135,
             "series": _series()} for _ in range(8)]
    assert rundynamics.build_trend(runs) is None


# ---------- the injury signal ----------

def test_drift_streak_counts_only_the_most_recent_consecutive_sessions():
    drift, clean = {"drift": True}, {"drift": False}
    assert rundynamics.drift_streak([drift, clean, drift, drift]) == 2
    assert rundynamics.drift_streak([drift, drift, clean]) == 0
    # A session with no dynamics data breaks the streak: an absent measurement is not
    # evidence of anything.
    assert rundynamics.drift_streak([drift, None, drift]) == 1
    assert rundynamics.drift_streak([]) == 0


def test_injury_radar_takes_the_drift_as_a_contributing_signal():
    a = injury.assess([], [], history_days=90,
                      dynamics_drift_streak=rundynamics.DRIFT_STREAK)
    kinds = {s.kind for s in a.signals}
    assert "dynamics" in kinds
    # Contributing, never a warning on its own — same rule as NF-24's grey zone.
    assert a.level != "high"


def test_one_drifting_session_is_not_a_signal():
    a = injury.assess([], [], history_days=90, dynamics_drift_streak=1)
    assert a.signals == []
    assert a.level == "none"


# ---------- tone: facts, not technique prescriptions ----------

def test_summary_reports_facts_without_prescribing_technique():
    dyn = rundynamics.session_dynamics(
        _series(cad=[182] * 30 + [170] * 30, gct=240, elev=100.0), dur_min=60)
    text = rundynamics.summary(dyn)
    assert "каденс" in text.lower()
    for banned in ("приземляйся", "тримай каденс", "збільш", "техніка"):
        assert banned not in text.lower()
