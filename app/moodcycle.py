"""NF-35 · daytime mood/energy check-in — weekly pattern + cycle-length hint.

Pure, zero-LLM read of ``app.db.lifestyle``'s daytime fields (``energy_level``/``mood``/
``irritability``, opt-in via ``User.mood_tracking_enabled``). Two questions, both
descriptive rather than diagnostic — this is a coach-side hint, not a health assessment:

* **Weekly pattern** — mean energy/mood/irritability by day-of-week, so "мій вівторок
  завжди важкий" becomes a number instead of a feeling. A weekday cell needs enough
  independent samples before it's shown; one bad Tuesday is noise, not a pattern.
* **Cycle hint** — autocorrelation of a daytime series across an 18-35 day lag window
  (only paired days where BOTH ends were answered count — same gap-tolerant pairing NF-02
  uses). A peak that clears the same significance bar the correlation engine uses is
  surfaced as "схоже, є цикл ~N днів", explicitly hedged: a few weeks of self-report can't
  separate a real biological cycle from a training-load or life-event coincidence, and the
  caller must say so rather than let a number read as a diagnosis.
"""
import datetime as dt
from typing import List, Optional

from app.correlations import R_THRESHOLD, pearson
from app.statutil import avg

MIN_HISTORY_DAYS = 21          # answered daytime entries before a cycle lag is even tested
MIN_WEEKDAY_SAMPLES = 2        # per-weekday cell before it's included in the pattern
CYCLE_MIN_LAG = 18
CYCLE_MAX_LAG = 35
MIN_CYCLE_SAMPLES = 15         # paired (d, d+lag) observations before a lag is tested

WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

_ENERGY_SCORE = {"charged": 3, "ok": 2, "tired": 1}


def _ci_excludes_zero(r: float, n: int) -> bool:
    """Same Fisher-z 95% CI check app.correlations uses — duplicated (not imported) since
    it's a three-line statistical primitive, not a shared dependency between the modules."""
    import math
    if n <= 3 or abs(r) >= 1.0:
        return abs(r) >= 1.0
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    return lo > 0 or hi < 0


def _numeric_series(logs: List[dict], field: str) -> List[tuple]:
    """``[(date, value), ...]`` for logs where ``field`` was actually answered."""
    out = []
    for row in logs:
        v = row.get(field)
        if field == "energy_level":
            v = _ENERGY_SCORE.get(v)
        if v is not None:
            out.append((row["date"], v))
    return out


def _weekday_pattern(series: List[tuple]) -> Optional[List[dict]]:
    buckets: dict = {i: [] for i in range(7)}
    for date_s, v in series:
        buckets[dt.date.fromisoformat(date_s).weekday()].append(v)
    if not any(len(v) >= MIN_WEEKDAY_SAMPLES for v in buckets.values()):
        return None
    return [
        {"label": WEEKDAY_LABELS[i], "avg": avg(buckets[i]), "n": len(buckets[i])}
        for i in range(7) if buckets[i]
    ]


def _cycle_hint(series: List[tuple]) -> Optional[dict]:
    """Best lag in ``[CYCLE_MIN_LAG, CYCLE_MAX_LAG]`` whose autocorrelation clears the same
    bar ``app.correlations`` uses, or ``None`` when nothing survives / there isn't enough
    span of answered days yet."""
    by_date = dict(series)
    if len(by_date) < MIN_HISTORY_DAYS:
        return None
    best = None
    for lag in range(CYCLE_MIN_LAG, CYCLE_MAX_LAG + 1):
        xs, ys = [], []
        for d, v in series:
            d2 = (dt.date.fromisoformat(d) + dt.timedelta(days=lag)).isoformat()
            if d2 in by_date:
                xs.append(v)
                ys.append(by_date[d2])
        if len(xs) < MIN_CYCLE_SAMPLES:
            continue
        r = pearson(xs, ys)
        if r is None or abs(r) < R_THRESHOLD or not _ci_excludes_zero(r, len(xs)):
            continue
        if best is None or abs(r) > abs(best["r"]):
            best = {"lag": lag, "r": round(r, 2), "n": len(xs)}
    return best


def analyze(logs: List[dict]) -> Optional[dict]:
    """``{"weekly": {...} | None, "cycle": {...} | None, "history_days": N}`` — either half
    absent when there's no signal or not enough history yet. ``None`` overall when nothing
    has ever been answered (the caller renders nothing rather than an empty section)."""
    series = {
        "mood": _numeric_series(logs, "mood"),
        "energy": _numeric_series(logs, "energy_level"),
        "irritability": _numeric_series(logs, "irritability"),
    }
    answered = {d for s in series.values() for d, _ in s}
    if not answered:
        return None

    weekly = {k: pat for k, s in series.items() if (pat := _weekday_pattern(s))}
    cycle = _cycle_hint(series["mood"]) or _cycle_hint(series["energy"])

    if not weekly and not cycle:
        return None
    return {"weekly": weekly or None, "cycle": cycle, "history_days": len(answered)}
