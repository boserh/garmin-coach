"""NF-01 personal baselines: the pure-Python percentile detector (band + position),
the min-samples gate, the most-recent-non-null "cur", and that norm enters the
dedup-cache key."""
from app import baselines
from app.analysis.service import _cache_key


def _history(values, key="resting_hr", start="2026-04-01"):
    """Build oldest-first daily rows carrying one metric (like repository.read_history)."""
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    return [
        {"date": (d0 + dt.timedelta(days=i)).isoformat(), key: v}
        for i, v in enumerate(values)
    ]


# --- percentile helper --------------------------------------------------------

def test_percentile_median_and_quartiles():
    s = [10, 20, 30, 40, 50]
    assert baselines._percentile(s, 0.5) == 30
    assert baselines._percentile(s, 0.25) == 20
    assert baselines._percentile(s, 0.75) == 40


def test_percentile_interpolates():
    assert baselines._percentile([0, 10], 0.5) == 5.0


def test_percentile_single_value():
    assert baselines._percentile([42], 0.5) == 42


# --- min-samples gate ---------------------------------------------------------

def test_below_min_samples_is_skipped():
    hist = _history([55] * (baselines.MIN_SAMPLES - 1))
    assert baselines.compute_baselines(hist) is None


def test_empty_history_returns_none():
    assert baselines.compute_baselines([]) is None


def test_at_min_samples_qualifies():
    hist = _history([55] * baselines.MIN_SAMPLES)
    norm = baselines.compute_baselines(hist)
    assert norm is not None
    assert "resting_hr" in norm["metrics"]
    assert norm["window_days"] == baselines.WINDOW_DAYS


# --- band / position / cur ----------------------------------------------------

def test_band_and_position_low():
    # 20 days ranging 50..69, then today a clearly-low 48 → below the p25 band.
    vals = list(range(50, 70)) + [48]
    hist = _history(vals)
    norm = baselines.compute_baselines(hist)
    m = norm["metrics"]["resting_hr"]
    assert m["cur"] == 48
    assert m["pos"] == "low"
    lo, hi = m["band"]
    assert lo <= m["p50"] <= hi
    assert m["n"] == len(vals)


def test_position_high():
    vals = list(range(50, 70)) + [80]
    norm = baselines.compute_baselines(_history(vals))
    assert norm["metrics"]["resting_hr"]["pos"] == "high"


def test_position_normal():
    vals = list(range(50, 70)) + [60]
    norm = baselines.compute_baselines(_history(vals))
    assert norm["metrics"]["resting_hr"]["pos"] == "normal"


def test_cur_is_most_recent_non_null():
    hist = _history([55] * baselines.MIN_SAMPLES)
    hist.append({"date": "2026-07-10", "resting_hr": None})  # today missing
    hist.append({"date": "2026-07-11", "resting_hr": 51})    # newest present
    norm = baselines.compute_baselines(hist)
    assert norm["metrics"]["resting_hr"]["cur"] == 51


def test_non_numeric_values_ignored():
    hist = _history([55] * baselines.MIN_SAMPLES)
    hist.append({"date": "2026-07-11", "resting_hr": "n/a"})  # junk, ignored
    norm = baselines.compute_baselines(hist)
    # cur falls back to the last numeric (55), junk not counted in n
    assert norm["metrics"]["resting_hr"]["cur"] == 55
    assert norm["metrics"]["resting_hr"]["n"] == baselines.MIN_SAMPLES


def test_sleep_h_rounds_to_one_decimal():
    vals = [7.234] * baselines.MIN_SAMPLES
    norm = baselines.compute_baselines(_history(vals, key="sleep_h"))
    assert norm["metrics"]["sleep_h"]["cur"] == 7.2


def test_multiple_metrics_and_partial_data():
    # resting_hr has enough days; hrv_avg does not → only resting_hr appears.
    rows = []
    import datetime as dt
    d0 = dt.date.fromisoformat("2026-04-01")
    for i in range(baselines.MIN_SAMPLES):
        row = {"date": (d0 + dt.timedelta(days=i)).isoformat(), "resting_hr": 55}
        if i < 3:
            row["hrv_avg"] = 60
        rows.append(row)
    norm = baselines.compute_baselines(rows)
    assert set(norm["metrics"]) == {"resting_hr"}


# --- dedup-cache key ----------------------------------------------------------

def test_norm_changes_cache_key():
    base = {"daily": [], "recent_activities": [], "planned_runs": []}
    norm = baselines.compute_baselines(_history([55] * baselines.MIN_SAMPLES))
    k_without = _cache_key(base, "q", "m")
    k_with = _cache_key(base, "q", "m", norm=norm)
    assert k_without != k_with
