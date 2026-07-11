"""Personal baselines (NF-01) — pure-Python, zero-LLM "today vs your norm".

Every recovery metric already lives in ``daily_metrics``; a number like "RHR 52" only
means something against *your own* history, season and training block — not a generic
scale. :func:`compute_baselines` turns a slice of daily history into rolling percentiles
(p25 / p50 / p75) per metric, so the morning report can say "today / your p50 / your band"
and flag where today sits. The LLM computes nothing — it only narrates the ready deviations.

No network, no Claude; cheap enough to build on every report (a few hundred scalar rows,
no per-minute arrays). Mirrors the ``records.py`` shape: a pure detector fed straight into
the Claude context (and the dedup-cache key — the README pitfall).

Scope (M): a single 90-day rolling window — the decision-relevant "your normal *recently*",
which tracks current fitness/season without the noise of stale years. Longer/seasonal
windows (the ticket's 30/365) are a future extension; percentiles are robust to the gaps a
backfill leaves.
"""
from typing import List, Optional, Sequence

# Metrics we baseline, with a display label + whether a higher value is the healthier
# direction. Valence is passed to the LLM (not used to compute position, which is neutral).
_METRICS = {
    "resting_hr":  {"label": "пульс спокою", "higher_better": False, "round": 0},
    "hrv_avg":     {"label": "HRV", "higher_better": True, "round": 0},
    "sleep_score": {"label": "оцінка сну", "higher_better": True, "round": 0},
    "sleep_h":     {"label": "сон", "higher_better": True, "round": 1},
    "stress_avg":  {"label": "середній стрес", "higher_better": False, "round": 0},
    "bb_charged":  {"label": "нічний заряд Body Battery", "higher_better": True, "round": 0},
}

# Need at least this many days of a metric before its band means anything (a handful of
# points gives a meaningless "band"). New users / sparse metrics are simply skipped.
MIN_SAMPLES = 14

# Rolling window: the last N days of history feed the percentiles.
WINDOW_DAYS = 90


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile over a non-empty, already-sorted list (numpy-free)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _round(value: float, ndigits: int) -> float:
    return round(value) if ndigits == 0 else round(value, ndigits)


def _position(cur: float, low: float, high: float) -> str:
    """Where today sits relative to the typical band — neutral (valence is the LLM's job)."""
    if cur < low:
        return "low"
    if cur > high:
        return "high"
    return "normal"


def compute_baselines(history: List[dict], *, min_samples: int = MIN_SAMPLES) -> Optional[dict]:
    """Rolling personal baselines from a list of daily rows (as ``repository.read_history``
    returns them: oldest-first dicts carrying the recovery scalars). Pure and side-effect
    free. Returns a compact ``norm`` snapshot for the Claude context, or ``None`` when no
    metric has enough history.

    Per metric: ``{cur, p50, band:[p25,p75], n, pos}``. ``cur`` is the most recent non-null
    value (today, or the last synced day); ``pos`` is low/normal/high vs the band.
    """
    if not history:
        return None

    out: dict = {}
    for key, cfg in _METRICS.items():
        vals: List[float] = [
            float(v) for r in history
            if isinstance((v := r.get(key)), (int, float))
        ]
        if len(vals) < min_samples:
            continue
        # current = most recent non-null (scan from the newest end)
        cur: Optional[float] = next(
            (float(v) for r in reversed(history)
             if isinstance((v := r.get(key)), (int, float))),
            None,
        )
        if cur is None:
            continue
        s = sorted(vals)
        p25, p50, p75 = _percentile(s, 0.25), _percentile(s, 0.50), _percentile(s, 0.75)
        nd = cfg["round"]
        out[key] = {
            "cur": _round(cur, nd),
            "p50": _round(p50, nd),
            "band": [_round(p25, nd), _round(p75, nd)],
            "n": len(vals),
            "pos": _position(cur, p25, p75),
        }

    if not out:
        return None
    return {"window_days": WINDOW_DAYS, "metrics": out}
