"""NF-19 · aerobic-efficiency trend — "am I running faster at the same heart rate?"

The most honest marker of aerobic progress is pace at a given HR, and it's the one signal
the product never surfaced: VO2max/race predictions are Garmin's black box with daily
jitter (EP-14/NF-10 already fight it with medians), and the raw per-point ``series`` (pace +
HR, years of it in the DB after the FIT backfill) sat unused between activities.

Pure Python, zero LLM, zero network (the series is already stored). For each *easy* run —
identified by its average HR sitting in the runner's own easy corridor (p25–p60 of their
run HRs), the risk-mitigation second gate from the ticket, rather than a plan label the
activity doesn't carry — of ≥``MIN_MINUTES`` with a valid HR, compute an efficiency factor
``EF = speed(m/min) / avg_HR`` using the **GAP-adjusted** pace (``gap.effective_pace_min_km``
— so a hilly week doesn't read as a fitness drop). Weekly-median EF over 12 weeks, then a
least-squares trend (reusing the same weekly-median + linear-fit shape as ``goal.py``).

Honesty gates: fewer than ``MIN_WEEKS`` weeks with data, or too small a sample, → a
``calibrating`` state with zero numbers (never a trend fit on noise). Temperature isn't
corrected for (a documented v1 limitation — summer weeks can dip from heat), and the
formatter says so.
"""
import datetime as dt
from typing import List, Optional

from app import gap
from app.baselines import _percentile

MIN_MINUTES = 30          # a run must be at least this long to count (steady aerobic effort)
MIN_WEEKS = 6             # need this many distinct weeks with an easy run before any trend
MIN_TOTAL_RUNS = 8        # ...and this many qualifying runs overall (≈2/week over the weeks)
EASY_HR_LO_Q = 0.25       # easy-corridor lower bound: p25 of the runner's own run HRs
EASY_HR_HI_Q = 0.60       # ...upper bound: p60 (above this it's a tempo/hard effort, excluded)
_PACE_FLOOR = 2.5         # sanity: reject GPS-junk paces faster than 2:30/km (as records.py)
_PACE_CEIL = 12.0


def _iso_week(date_s: Optional[str]) -> Optional[str]:
    try:
        return dt.date.fromisoformat(date_s).strftime("%G-W%V")
    except (TypeError, ValueError):
        return None


def _raw_pace(run: dict) -> Optional[float]:
    km, mins = run.get("dist_km"), run.get("dur_min")
    if not km or not mins or km <= 0:
        return None
    pace = mins / km
    return pace if _PACE_FLOOR <= pace <= _PACE_CEIL else None


def _easy_corridor(run_hrs: List[float]) -> Optional[tuple]:
    """The runner's own easy-HR band (p25–p60 of all their run average HRs), or None when
    there aren't enough runs to define one."""
    if len(run_hrs) < 3:
        return None
    s = sorted(run_hrs)
    return _percentile(s, EASY_HR_LO_Q), _percentile(s, EASY_HR_HI_Q)


def _ef(run: dict) -> Optional[float]:
    """Efficiency factor for one run: GAP-adjusted speed (m/min) per bpm. Higher = fitter
    (faster at the same HR). None when pace/HR/elevation math can't produce a clean value."""
    hr = run.get("avg_hr")
    raw = _raw_pace(run)
    if not hr or hr <= 0 or raw is None:
        return None
    pace = gap.effective_pace_min_km(run.get("series"), raw) or raw
    if not (_PACE_FLOOR <= pace <= _PACE_CEIL):
        return None
    speed_m_min = 1000.0 / pace           # min/km → m/min
    return speed_m_min / hr


def _linear_trend(points: List[float]) -> Optional[tuple]:
    """Least-squares (slope, intercept) over week ORDER — same numpy-free fit as goal.py."""
    n = len(points)
    if n < 2:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(points) / n
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, points))
    slope = num / den
    return slope, mean_y - slope * mean_x


def build_trend(runs: List[dict], *, weeks: int = 12) -> Optional[dict]:
    """Turn a list of run dicts (``{date, type, dur_min, dist_km, avg_hr, series}``) into an
    aerobic-efficiency trend. Returns:

    * ``None`` — no easy-run signal at all (say nothing).
    * ``{"status": "calibrating", "n_weeks": k}`` — some data, but under the honesty gates.
    * ``{"status": "ok", ...}`` — ``current_ef``, ``slope_per_week``, ``pct_change``,
      ``typical_hr``, ``delta_pace_s`` (pace change at that HR over the window, negative =
      faster), ``n_weeks``, ``weekly`` ([{week, ef}]).
    """
    long_enough = [
        r for r in runs
        if r.get("avg_hr") and (r.get("dur_min") or 0) >= MIN_MINUTES and _raw_pace(r) is not None
    ]
    corridor = _easy_corridor([float(r["avg_hr"]) for r in long_enough])
    if corridor is None:
        return None
    lo, hi = corridor
    easy = [r for r in long_enough if lo <= float(r["avg_hr"]) <= hi]
    if not easy:
        return None

    buckets: dict = {}
    hr_used: List[float] = []
    for r in easy:
        ef = _ef(r)
        week = _iso_week(r.get("date"))
        if ef is None or week is None:
            continue
        buckets.setdefault(week, []).append(ef)
        hr_used.append(float(r["avg_hr"]))

    total_runs = sum(len(v) for v in buckets.values())
    weekly = [{"week": w, "ef": round(sorted(v)[len(v) // 2], 4)}
              for w, v in sorted(buckets.items())]
    n_weeks = len(weekly)

    if n_weeks < MIN_WEEKS or total_runs < MIN_TOTAL_RUNS:
        return {"status": "calibrating", "n_weeks": n_weeks}

    fit = _linear_trend([w["ef"] for w in weekly])
    if fit is None:
        return {"status": "calibrating", "n_weeks": n_weeks}
    slope, intercept = fit
    first_ef = intercept                       # fitted EF at week 0
    current_ef = intercept + slope * (n_weeks - 1)
    typical_hr = round(sorted(hr_used)[len(hr_used) // 2])
    pct_change = round((current_ef - first_ef) / first_ef * 100.0, 1) if first_ef else 0.0

    # Pace change at the typical HR implied by the EF change (pace = 1000 / (EF·HR)).
    delta_pace_s = None
    if first_ef > 0 and current_ef > 0 and typical_hr:
        pace_start = 1000.0 / (first_ef * typical_hr)
        pace_now = 1000.0 / (current_ef * typical_hr)
        delta_pace_s = round((pace_now - pace_start) * 60.0)

    return {
        "status": "ok",
        "n_weeks": n_weeks,
        "current_ef": round(current_ef, 3),
        "slope_per_week": round(slope, 4),
        "pct_change": pct_change,
        "typical_hr": typical_hr,
        "delta_pace_s": delta_pace_s,
        "weekly": weekly,
    }


def _fmt_delta_s(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else "+"
    s = abs(int(seconds))
    return f"{sign}{s // 60}:{s % 60:02d}" if s >= 60 else f"{sign}{s}с"


def summary(trend: Optional[dict]) -> Optional[str]:
    """Deterministic Ukrainian text for ``/goal`` — no LLM. ``None`` when there's nothing
    to say; a short calibrating line when there's data but not enough."""
    if not trend:
        return None
    if trend["status"] == "calibrating":
        return ("🫀 Аеробна ефективність: ще калібруюсь "
                f"({trend['n_weeks']}/{MIN_WEEKS} тижнів з легкими бігами — потрібно більше).")
    pct = trend["pct_change"]
    arrow = "покращення" if pct > 0 else ("сповільнення" if pct < 0 else "стабільно")
    line = (f"🫀 Аеробна ефективність (легкі біги, {trend['n_weeks']} тиж, GAP-чесно): "
            f"{pct:+.1f}% — {arrow}")
    if trend["delta_pace_s"] is not None and trend["typical_hr"]:
        line += (f" ≈ {_fmt_delta_s(trend['delta_pace_s'])}/км "
                 f"при пульсі {trend['typical_hr']}")
    line += ".\nЛітні тижні можуть просідати через спеку (корекції на температуру нема)."
    return line
