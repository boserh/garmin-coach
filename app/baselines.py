"""Personal baselines (NF-01) — pure-Python, zero-LLM "today vs your norm".

Every recovery metric already lives in ``daily_metrics``; a number like "RHR 52" only
means something against *your own* history, season and training block — not a generic
scale. :func:`compute_baselines` turns a slice of daily history into rolling percentiles
(p25 / p50 / p75) per metric, so the morning report can say "today / your p50 / your band"
and flag where today sits. The LLM computes nothing — it only narrates the ready deviations.

No network, no Claude; cheap enough to build on every report (a few hundred scalar rows,
no per-minute arrays). Mirrors the ``records.py`` shape: a pure detector fed straight into
the Claude context (and the dedup-cache key — the README pitfall).

Two windows, on purpose. A 90-day percentile is a *stable* reference — which is exactly
what the detectors that read ``band`` as a threshold (``health.detect``, ``sleepnudge``)
want, but it is also why the morning report used to quote the identical median every
single morning for weeks: with ~90 samples, one day rolling in and one rolling out moves
the middle rank by at most one position, and recovery scalars are integers clustered on
three or four values. That is arithmetic, not a bug — but a "median" that never moves is
dead weight in a daily narration. So each metric also carries a short
:data:`RECENT_DAYS` window (``recent``) plus the signed drift between the two (``trend``),
which is what the report actually narrates: "your norm *now*", and whether it moved.

``cur`` additionally reports how old it is (``stale_days``) — Garmin fills some metrics
late, so the most recent non-null can be yesterday's or older, and narrating it as
"today" is a lie the LLM had no way to catch.
"""
import datetime as dt
from typing import Any, List, Optional, Sequence, Tuple

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

# The short, responsive window narrated as "your norm now". Long enough to survive a bad
# week and the gaps a sync leaves, short enough that a real drift in fitness or season
# shows up within weeks instead of being averaged away by a quarter of history.
RECENT_DAYS = 28

# A short window needs its own (lower) floor — 28 days of history rarely carry 28 samples.
RECENT_MIN_SAMPLES = 10


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


def _date(value: Any) -> Optional[dt.date]:
    """Parse a row's ISO ``date``; None for a missing/rubbish one (rows stay usable)."""
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _values(rows: Sequence[dict], key: str) -> List[float]:
    return [float(v) for r in rows if isinstance((v := r.get(key)), (int, float))]


def _recent_rows(history: Sequence[dict], days: int) -> List[dict]:
    """The tail of ``history`` covering the last ``days`` calendar days.

    Sliced by date, not by position: ``read_history`` returns one row per *stored* day, so
    a positional tail silently reaches months back whenever the sync left a gap.
    Falls back to the positional tail when no row carries a parseable date (tests, and any
    caller that builds rows by hand)."""
    dated = [(d, r) for r in history if (d := _date(r.get("date"))) is not None]
    if not dated:
        return list(history[-days:])
    cutoff = max(d for d, _ in dated) - dt.timedelta(days=days - 1)
    return [r for d, r in dated if d >= cutoff]


def _current(history: Sequence[dict], key: str) -> Tuple[Optional[float], Optional[int]]:
    """The most recent non-null value of ``key`` and how many days stale it is.

    Staleness is measured against the newest dated row in the slice (the day the caller
    thinks of as "today"), so a metric Garmin hasn't filled in yet is reported as what it
    is — an older reading — instead of being narrated as this morning's."""
    newest = max((d for r in history if (d := _date(r.get("date"))) is not None), default=None)
    for row in reversed(history):
        v = row.get(key)
        if isinstance(v, (int, float)):
            cur_date = _date(row.get("date"))
            stale = (newest - cur_date).days if newest and cur_date else None
            return float(v), (stale if stale else None)
    return None, None


def compute_baselines(history: List[dict], *, min_samples: int = MIN_SAMPLES) -> Optional[dict]:
    """Rolling personal baselines from a list of daily rows (as ``repository.read_history``
    returns them: oldest-first dicts carrying the recovery scalars). Pure and side-effect
    free. Returns a compact ``norm`` snapshot for the Claude context, or ``None`` when no
    metric has enough history.

    Per metric: ``{cur, p50, band:[p25,p75], n, pos}`` over the whole slice — the stable
    long reference every threshold reader (``health``, ``sleepnudge``, the dashboard ring)
    consumes, unchanged. ``cur`` is the most recent non-null value (today, or the last
    synced day); ``pos`` is low/normal/high vs that band.

    Plus, when the last :data:`RECENT_DAYS` days hold enough samples of their own:

    * ``recent`` — ``{p50, band, n, days}`` over that short window: "your norm *now*",
      the one the daily report narrates (see the module docstring on why the 90-day
      median reads as frozen);
    * ``trend`` — signed ``recent.p50 - p50``, present only when the drift survives the
      metric's own rounding (a 0.0 drift is noise, not a finding);
    * ``pos_recent`` — where ``cur`` sits against the *recent* band.

    ``stale_days`` appears only when ``cur`` predates the newest day in the slice.
    """
    if not history:
        return None

    recent_rows = _recent_rows(history, RECENT_DAYS)

    out: dict = {}
    for key, cfg in _METRICS.items():
        vals = _values(history, key)
        if len(vals) < min_samples:
            continue
        cur, stale_days = _current(history, key)
        if cur is None:
            continue
        s = sorted(vals)
        p25, p50, p75 = _percentile(s, 0.25), _percentile(s, 0.50), _percentile(s, 0.75)
        nd = cfg["round"]
        entry = {
            "cur": _round(cur, nd),
            "p50": _round(p50, nd),
            "band": [_round(p25, nd), _round(p75, nd)],
            "n": len(vals),
            "pos": _position(cur, p25, p75),
        }
        if stale_days:
            entry["stale_days"] = stale_days

        r_vals = _values(recent_rows, key)
        if len(r_vals) >= RECENT_MIN_SAMPLES:
            rs = sorted(r_vals)
            r25, r50, r75 = _percentile(rs, 0.25), _percentile(rs, 0.50), _percentile(rs, 0.75)
            entry["recent"] = {
                "p50": _round(r50, nd),
                "band": [_round(r25, nd), _round(r75, nd)],
                "n": len(r_vals),
                "days": RECENT_DAYS,
            }
            entry["pos_recent"] = _position(cur, r25, r75)
            trend = _round(r50 - p50, nd)
            if trend:
                entry["trend"] = trend

        out[key] = entry

    if not out:
        return None
    return {"window_days": WINDOW_DAYS, "recent_days": RECENT_DAYS, "metrics": out}
