"""NF-19 · aerobic-efficiency trend — pure module (EF, easy-run gate, GAP-honesty,
calibrating gate) plus the digest-context/cache wiring (Claude mocked)."""
import datetime as dt

from app import efficiency


def _flat_series(pace=6.0, km=8.0, n=40):
    return [{"d": km * i / (n - 1), "p": pace, "hr": 140, "e": 0.0} for i in range(n)]


def _hilly_series(pace=6.4, km=8.0, gain_m=200.0, n=40):
    return [{"d": km * i / (n - 1), "p": pace, "hr": 140, "e": gain_m * i / (n - 1)}
            for i in range(n)]


def _run(date, *, pace=6.0, hr=140, dur=None, km=8.0, series=None, type="running"):
    # avg pace (dur/km) drives EF; keep dur consistent with the intended pace unless the
    # caller overrides it (the short-run gate test).
    dur = dur if dur is not None else pace * km
    return {"date": date, "type": type, "dur_min": dur, "dist_km": km, "avg_hr": hr,
            "series": series if series is not None else _flat_series(pace, km)}


def _weeks(n, start="2026-04-06", **run_kw):
    """One run per week for n weeks (Mondays), all easy (same HR)."""
    d = dt.date.fromisoformat(start)
    return [_run((d + dt.timedelta(weeks=i)).isoformat(), **run_kw) for i in range(n)]


# ---------- EF + gating ----------

def test_short_or_no_hr_runs_excluded():
    # only short / no-HR runs → no corridor → None
    runs = [_run("2026-04-06", dur=20, km=3.0), {"date": "2026-04-07", "type": "running",
            "dur_min": 50, "dist_km": 8, "avg_hr": None, "series": _flat_series()}]
    assert efficiency.build_trend(runs) is None


def test_calibrating_when_too_few_weeks():
    t = efficiency.build_trend(_weeks(4))
    assert t["status"] == "calibrating" and t["n_weeks"] == 4


def test_ok_trend_over_enough_weeks():
    # 8 weeks, EF improving (pace getting faster week over week at the same HR)
    runs = []
    d = dt.date.fromisoformat("2026-04-06")
    for i in range(8):
        runs.append(_run((d + dt.timedelta(weeks=i)).isoformat(), pace=6.4 - i * 0.05))
    t = efficiency.build_trend(runs)
    assert t["status"] == "ok"
    assert t["n_weeks"] == 8
    assert t["pct_change"] > 0            # improving
    assert t["delta_pace_s"] < 0          # faster at the same HR
    assert t["typical_hr"] == 140


def test_hard_runs_excluded_by_hr_corridor():
    # a batch of easy runs (hr 140) + a few hard ones (hr 175); the hard ones sit above the
    # p60 corridor and must not enter the EF sample.
    runs = _weeks(8, pace=6.0, hr=140)
    runs += [_run("2026-06-01", pace=4.5, hr=178), _run("2026-06-08", pace=4.4, hr=180)]
    t = efficiency.build_trend(runs)
    assert t["status"] == "ok"
    # typical HR reflects the easy corridor, not the hard sessions
    assert t["typical_hr"] <= 150


# ---------- GAP honesty ----------

def test_gap_brings_hilly_and_flat_ef_closer():
    """The same effort run flat vs uphill should read as similar EF once GAP-adjusted —
    closer than the raw pace would make them (the AC's terrain-honesty test). We build the
    flat run at the hilly run's OWN GAP-equivalent pace, so a correct GAP makes the two EFs
    coincide while the raw split would put them apart."""
    from app import gap

    hilly_series = _hilly_series(6.4)
    gap_pace = gap.activity_gap_pace_min_km(hilly_series)   # the flat-equivalent pace uphill
    assert gap_pace is not None and gap_pace < 6.4          # climbing → GAP is faster-equiv

    flat = _run("2026-05-01", pace=gap_pace, hr=140, series=_flat_series(gap_pace))
    hilly = _run("2026-05-02", pace=6.4, hr=140, series=hilly_series)

    ef_flat = efficiency._ef(flat)
    ef_hilly_gap = efficiency._ef(hilly)
    ef_hilly_raw = (1000.0 / 6.4) / 140   # what EF would be WITHOUT GAP (raw split)

    assert abs(ef_hilly_gap - ef_flat) < abs(ef_hilly_raw - ef_flat)


# ---------- summary ----------

def test_summary_states():
    assert efficiency.summary(None) is None
    cal = efficiency.summary({"status": "calibrating", "n_weeks": 3})
    assert "калібр" in cal
    ok = efficiency.summary({"status": "ok", "n_weeks": 10, "pct_change": 3.2,
                             "delta_pace_s": -10, "typical_hr": 148, "current_ef": 1.2,
                             "slope_per_week": 0.01})
    assert "148" in ok and "+3.2%" in ok
