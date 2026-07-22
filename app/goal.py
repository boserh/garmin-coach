"""NF-10 · `/goal` — quantified progress toward a training plan's target.

A weekly digest can say "відстаєш" qualitatively, but never puts a number on it: what's
the current race-time prediction, where is the trend heading, and does it land on target
by race day? Every input already lives in ``daily_metrics.extra`` (Garmin's own race-time
predictions + VO2max, refreshed every few days) and ``TrainingPlan`` (the goal + target
date) — this module is just the missing trend math.

Pure Python, zero LLM, zero network (mirrors ``compare.py``/``wrapped.py``'s shape): a
context builder (:func:`project`) plus a deterministic Ukrainian formatter
(:func:`summary`), so ``/goal`` costs nothing to run. A plan's ``goal`` maps to a
Garmin metric to track (:func:`metric_for_goal`) — a race distance for a race goal,
VO2max (general fitness) for the open-ended "just keep training" goal.

Honesty caveats (deliberate, not bugs): there is currently no time-target input on the
plan-setup form, so the "on track by race day" verdict never fires today — ``/goal``
always shows the trend, not a binary "встигаєш" — the ``target_s`` parameter exists for
when that input lands. Garmin's race-time prediction is a fitness *proxy*, not a real
result — the formatter says so explicitly. A linear extrapolation is only honest over a
short horizon; a target further than ``FAR_HORIZON_WEEKS`` away shows the trend without
projecting to a number (the ticket's own pitfall: don't promise a number that far out).
"""
import datetime as dt
from typing import List, Optional, Tuple

MIN_WEEKS = 3            # need at least this many distinct weeks of readings for a trend
FAR_HORIZON_WEEKS = 12   # beyond this, show the trend only — no projected number
CLOSE_MARGIN_PCT = 0.03  # within 3% of a time target still counts as "close"

# plan.goal → (extra key, Ukrainian label, higher_is_better). Race goals track Garmin's
# own prediction for that distance (lower = faster = better); the open-ended "general"
# goal (no target race) falls back to VO2max — a general fitness trend.
GOAL_METRIC = {
    "first_5k":   ("race_5k_s", "прогноз на 5 км", False),
    "faster_5k":  ("race_5k_s", "прогноз на 5 км", False),
    "first_10k":  ("race_10k_s", "прогноз на 10 км", False),
    "first_half": ("race_half_s", "прогноз на півмарафон", False),
}
_DEFAULT_METRIC = ("vo2max", "VO2max (загальна форма)", True)


def metric_for_goal(goal: Optional[str]) -> Tuple[str, str, bool]:
    """The ``(metric_key, label, higher_is_better)`` this goal's progress is tracked by —
    a race-time prediction for a race goal, VO2max for anything without one (incl. the
    open-ended ``general`` goal, or an unrecognised/legacy goal string)."""
    return GOAL_METRIC.get(goal or "", _DEFAULT_METRIC)


def _iso_week(date_s: Optional[str]) -> Optional[str]:
    try:
        return dt.date.fromisoformat(date_s).strftime("%G-W%V")
    except (TypeError, ValueError):
        return None


def weekly_medians(history: List[dict], metric_key: str) -> List[dict]:
    """``[{week, median}]`` oldest-first, one entry per ISO week that had ≥1 reading for
    ``metric_key`` — the daily-jitter smoothing race predictions need (a fresh GPS-derived
    prediction can swing by seconds day to day; EP-14 uses the same ≥10s-margin reasoning
    for exactly this noise). ``history`` is ``repository.read_fitness_history``'s shape;
    a backfill gap just skips a week bucket rather than skewing anything."""
    buckets: dict = {}
    for row in history:
        v = row.get(metric_key)
        week = _iso_week(row.get("date"))
        if v is None or week is None:
            continue
        buckets.setdefault(week, []).append(float(v))
    return [{"week": w, "median": sorted(vals)[len(vals) // 2]}
            for w, vals in sorted(buckets.items())]


def _linear_trend(points: List[float]) -> Optional[Tuple[float, float]]:
    """Least-squares ``(slope, intercept)`` over an evenly-spaced index (week ORDER, not
    calendar week number — a skipped week just shortens the series instead of distorting
    the fit). ``None`` when there's nothing to fit (fewer than 2 points, or a degenerate
    (all-same-index) series, which can't happen here but is guarded anyway)."""
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


def project(
    history: List[dict], *, metric_key: str, higher_better: bool = False,
    target_s: Optional[float] = None, target_date: Optional[str] = None,
    today: Optional[dt.date] = None,
) -> Optional[dict]:
    """Fit a weekly-median trend for ``metric_key`` and, if ``target_date`` is close
    enough to extrapolate honestly, project it forward. Returns ``None`` when there's
    under :data:`MIN_WEEKS` distinct weeks of data (the "not enough history yet" path —
    callers show a friendly message, never a guess).

    Returns ``{metric, current, slope_per_week, projected, weeks_to_target, target_s,
    verdict, n_weeks}``. ``projected``/``weeks_to_target`` are ``None`` without a
    ``target_date`` or when it's further out than :data:`FAR_HORIZON_WEEKS` (a linear
    fit isn't honest that far ahead — the trend numbers alone still are). ``verdict``
    (``on_track``/``close``/``behind``) is set only when BOTH a ``target_s`` and a
    within-horizon projection exist — today the plan-setup form has no time-target
    input, so this is ``None`` in practice; the caller then shows the trend on its own.
    """
    weekly = weekly_medians(history, metric_key)
    if len(weekly) < MIN_WEEKS:
        return None
    fit = _linear_trend([w["median"] for w in weekly])
    if fit is None:
        return None
    slope, _intercept = fit
    current = weekly[-1]["median"]

    today = today or dt.date.today()
    weeks_to_target = None
    projected = None
    if target_date:
        try:
            days = (dt.date.fromisoformat(target_date) - today).days
        except (TypeError, ValueError):
            days = None
        if days is not None:
            wk = max(0.0, days / 7.0)
            if wk <= FAR_HORIZON_WEEKS:
                weeks_to_target = round(wk, 1)
                projected = current + slope * wk

    verdict = None
    if target_s is not None and projected is not None:
        # "ahead" (better than target) always counts as on_track regardless of direction.
        ahead = projected <= target_s if not higher_better else projected >= target_s
        if ahead:
            verdict = "on_track"
        else:
            margin = target_s * CLOSE_MARGIN_PCT
            close = (projected <= target_s + margin) if not higher_better \
                else (projected >= target_s - margin)
            verdict = "close" if close else "behind"

    return {
        "metric": metric_key,
        "current": round(current, 2),
        "slope_per_week": round(slope, 2),
        "projected": round(projected, 2) if projected is not None else None,
        "weeks_to_target": weeks_to_target,
        "target_date": target_date,
        "target_s": target_s,
        "higher_better": higher_better,
        "verdict": verdict,
        "n_weeks": len(weekly),
    }


def fmt_time(seconds: Optional[float]) -> str:
    """A race-prediction value (seconds) as ``m:ss`` / ``h:mm:ss``. Not for VO2max."""
    if seconds is None:
        return "—"
    s = int(round(abs(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    sign = "-" if seconds < 0 else ""
    return f"{sign}{h}:{m:02d}:{sec:02d}" if h else f"{sign}{m}:{sec:02d}"


_VERDICT_LABEL = {"on_track": "у графіку 🟢", "close": "близько 🟡", "behind": "відстаєш 🔴"}


def summary(proj: dict, *, label: str) -> str:
    """Deterministic Ukrainian text for ``/goal`` and the digest fallback — no LLM. A race
    metric (``metric`` starting with ``race_``) formats as a time; VO2max as a bare number."""
    is_time = proj["metric"].startswith("race_")
    fmt = fmt_time if is_time else (lambda v: f"{v:g}" if v is not None else "—")

    lines = [f"📈 {label.capitalize()}: {fmt(proj['current'])} "
             f"(за останні {proj['n_weeks']} тижні з даними)"]
    slope = proj["slope_per_week"]
    improving = (slope < 0) if not proj["higher_better"] else (slope > 0)
    if abs(slope) < (1 if is_time else 0.05):
        lines.append("Тренд: стабільно, без явної зміни.")
    else:
        direction = "покращення" if improving else "сповільнення"
        lines.append(f"Тренд: {direction} ~{fmt(abs(slope))} на тиждень.")

    if proj["projected"] is not None:
        line = f"Проєкція через ~{proj['weeks_to_target']:.0f} тиж.: {fmt(proj['projected'])}"
        if proj["target_s"] is not None:
            verdict_label = _VERDICT_LABEL.get(proj["verdict"], "")
            line += f" проти цілі {fmt(proj['target_s'])} — {verdict_label}"
        lines.append(line)
    elif proj["target_date"] is None:
        lines.append("Ціль без дати — орієнтуйся на тренд форми вище.")
    else:
        lines.append("До цілі ще задалеко для точної проєкції — стеж за трендом вище.")

    lines.append("Це прогноз Garmin (проксі форми), не гарантований результат забігу.")
    return "\n".join(lines)
