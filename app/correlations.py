"""NF-02 · Correlation engine — "what actually affects you".

A monthly, **pure-Python, zero-LLM** pass over the daily-metric history already in the DB:
test a fixed set of lagged metric pairs (better sleep → next-day HRV, stress → HRV, resting
HR → HRV …) for a real, personal association, and keep only the statistically defensible
ones. The survivors — 1–2 non-obvious personal findings a month — are exactly the
"surprising insight" people miss in Garmin Connect; :func:`app.analysis.reports.run_insights`
turns them into one Sonnet "monthly insight".

The whole risk is **false positives on small N**, so the gates are conservative: a minimum
sample count, a meaningful effect size, AND a Fisher-z 95% confidence interval that excludes
zero. Nothing survives → an honest "not enough data yet" (the caller sends nothing). numpy-free
and robust to the gaps a backfill leaves (a pair is only sampled on days both metrics exist).

Only the statistics live here; the DB read is ``repository.read_history`` and the narration is
in the service — so this stays trivially unit-testable.
"""
import datetime as dt
import math
from typing import List, Optional

from app.statutil import mean

# Gates against small-N false positives.
MIN_SAMPLES = 30       # paired observations required before a pair is even considered
R_THRESHOLD = 0.35     # minimum |Pearson r| — a weak wisp isn't worth surfacing
CI_EXCLUDES_ZERO = True  # additionally require the Fisher-z 95% CI to exclude 0

# Human labels for the metrics we correlate (from repository.read_history's dict shape).
_NAMES = {
    "sleep_score": "оцінка сну",
    "sleep_h": "тривалість сну",
    "hrv_avg": "HRV",
    "stress_avg": "середній стрес",
    "bb_charged": "заряд Body Battery",
    "resting_hr": "пульс спокою",
}

# The candidate pairs: (x_metric, lag_days, y_metric). x on day d vs y on day d+lag. Chosen
# to be plausibly causal and non-obvious — the LLM narrates meaning; we only test association.
_PAIRS = [
    ("sleep_score", 0, "hrv_avg"),      # better sleep ↔ same-day HRV
    ("sleep_score", 1, "hrv_avg"),      # sleep today ↔ HRV tomorrow
    ("stress_avg", 1, "hrv_avg"),       # stress today ↔ HRV tomorrow
    ("resting_hr", 1, "hrv_avg"),       # resting HR today ↔ HRV tomorrow
    ("stress_avg", 0, "sleep_score"),   # daytime stress ↔ that night's sleep
    ("sleep_h", 0, "stress_avg"),       # sleep length ↔ next day's stress load
    ("bb_charged", 0, "hrv_avg"),       # overnight recharge ↔ HRV
    ("hrv_avg", 1, "sleep_score"),      # HRV today ↔ sleep tomorrow
]


def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation of two equal-length samples, or None when it's undefined
    (fewer than 2 points, or a constant series → zero variance)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _fisher_ci_excludes_zero(r: float, n: int) -> bool:
    """True when the 95% CI for the correlation (via Fisher's z-transform) does not straddle
    zero — a pure-Python significance check that needs no scipy. Requires n > 3."""
    if n <= 3 or abs(r) >= 1.0:
        return abs(r) >= 1.0  # perfect correlation is trivially non-zero
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    return lo > 0 or hi < 0


def _paired(history: List[dict], x: str, y: str, lag: int):
    """Extract the (x[d], y[d+lag]) samples where BOTH values exist. ``history`` is oldest
    first (repository.read_history) and may have day gaps — we index by ISO date so a gap
    simply drops that lagged pair rather than misaligning the series."""
    by_date = {row["date"]: row for row in history if row.get("date")}
    xs: List[float] = []
    ys: List[float] = []
    for row in history:
        date_s = row.get("date")
        xv = row.get(x)
        if date_s is None or xv is None:
            continue
        if lag == 0:
            target = row
        else:
            try:
                d2 = (dt.date.fromisoformat(date_s) + dt.timedelta(days=lag)).isoformat()
            except (TypeError, ValueError):
                continue
            target = by_date.get(d2)
        if target is None:
            continue
        yv = target.get(y)
        if yv is None:
            continue
        xs.append(float(xv))
        ys.append(float(yv))
    return xs, ys


def _finding(x: str, lag: int, y: str, r: float, n: int) -> dict:
    """A structured, human-readable finding for one significant pair."""
    direction = "позитивна" if r > 0 else "негативна"
    lag_txt = "того ж дня" if lag == 0 else f"через {lag} дн"
    return {
        "x": x,
        "y": y,
        "lag": lag,
        "r": round(r, 2),
        "n": n,
        "direction": direction,
        "detail": (f"{_NAMES.get(x, x)} → {_NAMES.get(y, y)} ({lag_txt}): "
                   f"{direction} кореляція r={round(r, 2)}, n={n}"),
    }


# ---------- NF-28: self-reported lifestyle tags as binary variables ----------

# A tag is only tested when BOTH classes (tagged days and untagged days) reach this count.
# Without it, three logged beers produce "alcohol improves your sleep, r=0.9" — the single
# most likely way this feature could produce confident nonsense.
MIN_TAG_OBSERVATIONS = 5

# What a lifestyle tag is tested against, and how the effect reads in the finding. Lag 1
# only: a tag is logged in the EVENING of day d, so its effect lands on the row for d+1 —
# that row's sleep score is that night's sleep and its HRV is the next morning's reading.
# Testing lag 0 as well would mostly re-test "how did today go" against a fact recorded at
# the end of today, which is the causally backwards direction.
_TAG_TARGETS = [
    ("hrv_avg", "HRV", "мс"),
    ("sleep_score", "оцінка сну", "п"),
    ("resting_hr", "пульс спокою", "уд/хв"),
]

# Findings must read as association, never causation and never judgement — this suffix is
# part of the contract (tests assert on it), not decoration.
ASSOCIATION_NOTE = "асоціація, не причина"


def merge_lifestyle(history: List[dict], logs: List[dict]) -> List[dict]:
    """Return ``history`` rows with one ``tag:<slug>`` 0/1 field per logged day.

    Only days that were actually logged get fields: a day with no row is *unknown*, not
    "nothing happened", and folding the two together would quietly inflate the control
    group with every day the user ignored the prompt."""
    from app.db import lifestyle as lifestyle_db

    by_date = {row.get("date"): row for row in logs if row.get("date")}
    out = []
    for row in history:
        entry = dict(row)
        log = by_date.get(row.get("date"))
        if log is not None:
            tags = set(log.get("tags") or [])
            for slug in lifestyle_db.TAG_ORDER:
                entry[f"tag:{slug}"] = 1.0 if slug in tags else 0.0
        out.append(entry)
    return out


def _tag_finding(slug: str, y: str, r: float, n: int,
                 mean_with: float, mean_without: float, unit: str, y_name: str) -> dict:
    """A lifestyle finding, phrased as an observed difference between two groups of the
    user's own nights. Never "X ruins your sleep" (causal) and never "you drink too much"
    (judgement) — just the number and the honest caveat."""
    from app.db import lifestyle as lifestyle_db

    delta = mean_with - mean_without
    direction = "нижч" if delta < 0 else "вищ"
    return {
        "x": f"tag:{slug}",
        "y": y,
        "lag": 1,
        "r": round(r, 2),
        "n": n,
        "kind": "lifestyle",
        "delta": round(delta, 1),
        "direction": "негативна" if r < 0 else "позитивна",
        "detail": (f"у ночі після «{lifestyle_db.label(slug)}» {y_name} в середньому на "
                   f"{abs(delta):.1f} {unit} {direction}ий "
                   f"({mean_with:.1f} проти {mean_without:.1f}, n={n}) — "
                   f"{ASSOCIATION_NOTE}"),
    }


def _tag_correlations(history: List[dict]) -> List[dict]:
    """Test every logged tag against the next day's recovery metrics. Same statistical
    gates as the metric pairs, plus the per-class observation floor."""
    from app.db import lifestyle as lifestyle_db

    out: List[dict] = []
    for slug in lifestyle_db.TAG_ORDER:
        x = f"tag:{slug}"
        for y, y_name, unit in _TAG_TARGETS:
            xs, ys = _paired(history, x, y, 1)
            n = len(xs)
            if n < MIN_SAMPLES:
                continue
            with_vals = [v for flag, v in zip(xs, ys) if flag >= 0.5]
            without_vals = [v for flag, v in zip(xs, ys) if flag < 0.5]
            if min(len(with_vals), len(without_vals)) < MIN_TAG_OBSERVATIONS:
                continue
            r = pearson(xs, ys)
            if r is None or abs(r) < R_THRESHOLD:
                continue
            if CI_EXCLUDES_ZERO and not _fisher_ci_excludes_zero(r, n):
                continue
            out.append(_tag_finding(slug, y, r, n, mean(with_vals), mean(without_vals),
                                    unit, y_name))
    return out


def find_correlations(history: List[dict],
                      lifestyle_logs: Optional[List[dict]] = None) -> List[dict]:
    """Test every candidate pair over ``history`` and return the significant findings
    (strongest |r| first). A pair passes only with enough samples, a meaningful effect
    size, and (by default) a Fisher-z CI that excludes zero — so noise on thin data is
    filtered out, not surfaced.

    NF-28: with ``lifestyle_logs`` the user's own evening tags join the candidate set as
    binary variables against the NEXT day's recovery metrics, under one extra gate — both
    classes need :data:`MIN_TAG_OBSERVATIONS` days before a tag is tested at all."""
    if lifestyle_logs:
        history = merge_lifestyle(history, lifestyle_logs)
    out: List[dict] = []
    for x, lag, y in _PAIRS:
        xs, ys = _paired(history, x, y, lag)
        n = len(xs)
        if n < MIN_SAMPLES:
            continue
        r = pearson(xs, ys)
        if r is None or abs(r) < R_THRESHOLD:
            continue
        if CI_EXCLUDES_ZERO and not _fisher_ci_excludes_zero(r, n):
            continue
        out.append(_finding(x, lag, y, r, n))
    if lifestyle_logs:
        out.extend(_tag_correlations(history))
    out.sort(key=lambda f: abs(f["r"]), reverse=True)
    return out


def build_context(findings: List[dict], window_days: int) -> dict:
    """Assemble the Claude context: the significant findings + the window they came from.
    The LLM computes nothing — it explains what these associations plausibly mean and stays
    honest that correlation isn't causation.

    NF-28 findings are split out under their own key so the prompt can give them their own
    section ("що на тебе діє") — they answer a different question from the metric-to-metric
    ones, and they are the findings most likely to be misread as a verdict on the user."""
    lifestyle = [f for f in findings if f.get("kind") == "lifestyle"]
    return {
        "window_days": window_days,
        "findings": [f for f in findings if f.get("kind") != "lifestyle"],
        "lifestyle_findings": lifestyle,
    }
