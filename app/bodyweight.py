"""NF-26 · Body weight — a trend, a race-day projection, and an under-fuelling signal.

Garmin Connect keeps every weigh-in (an Index scale, a manual entry), and until this nothing
read them. Weight matters to a runner in three ways this module serves, and in no other:

* the **trend** — a daily weigh-in swings ±1 kg on water and salt, so a raw point is useless
  and only a smoothed line says anything (:func:`ema`, a time-aware exponential average:
  irregular weigh-ins are weighted by the days between them, not by their count);
* the **rate** — kg/week, from a Theil–Sen slope (the median of pairwise slopes) over the
  last :data:`RATE_WINDOW_DAYS`. Robust by construction: one salty-dinner +2 kg reading moves
  a least-squares fit, it barely moves a median. That robustness is why this does not reuse
  ``goal._linear_trend`` (an OLS fit over weekly medians, where two weigh-ins a week make the
  "median" the higher of the two). The *horizon* rule is reused: no projected number further
  out than ``goal.FAR_HORIZON_WEEKS``;
* the **flags** — weight falling faster than :data:`FAST_LOSS_PCT_WEEK` of body mass a week,
  and weight falling while the training load climbs. That pair is the classic precursor of a
  bone-stress injury and of a hormonal crash, and nothing else in the app could see it.

**Tone is part of the contract.** This is a sensitive subject: the module states numbers and
directions and never a judgement — no "ideal", no "overweight", no advice to lose or gain.
The daily report gets a line *only when a flag fires* (a daily weight line in a coaching
message is a trigger, not a feature), and :func:`to_context` carries a fixed instruction
saying how to talk about it. Pure, zero LLM, zero network.
"""
import datetime as dt
import math
from typing import List, Optional, Tuple

from app import goal as goal_mod

EMA_TAU_DAYS = 10.0          # smoothing time constant: a lone reading moves the line ~10%
TREND_WINDOW_DAYS = 42       # the trend (and the dashboard tile) look this far back
MIN_TREND_POINTS = 3         # fewer weigh-ins than this in the window → no trend at all
RATE_WINDOW_DAYS = 28        # the kg/week rate is fitted over this window…
RATE_MIN_POINTS = 4          # …from at least this many weigh-ins…
RATE_MIN_SPAN_DAYS = 10      # …spanning at least this many days, or it is None
FAST_LOSS_PCT_WEEK = 1.0     # losing more than this % of body mass a week → flag
LOSS_ON_VOLUME_PCT_WEEK = 0.5  # a smaller loss still counts when the load is climbing…
VOLUME_RISE_PCT = 10.0       # …by at least this much against the athlete's usual

# Fixed wording the analyst receives with the flag — part of the contract, asserted in tests.
TONE_NOTE = ("Описово і нейтрально: лише напрямок і темп зміни ваги на тлі навантаження. "
             "Без оцінок ваги чи зовнішності, без норм і без порад схуднути чи набрати; "
             "за потреби — запропонувати звірити харчування і відновлення.")

# DailyMetric.extra keys this feature owns (written by app.garmin.service).
BODY_KEYS = ("weight_kg", "body_fat_pct", "body_water_pct", "muscle_kg", "bone_kg", "bmi")


def points(history: List[dict], *, today: Optional[dt.date] = None,
           days: int = TREND_WINDOW_DAYS) -> List[Tuple[dt.date, float]]:
    """``[(date, kg)]`` oldest first from ``read_history``-shaped rows (a top-level
    ``weight_kg`` or one inside ``extra``), limited to the last ``days`` days. Junk values
    (non-numeric, outside 25–300 kg) are dropped rather than trusted."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=days - 1)
    out = []
    for row in history or []:
        v = row.get("weight_kg")
        if v is None:
            v = (row.get("extra") or {}).get("weight_kg")
        try:
            d = dt.date.fromisoformat(row.get("date"))
            kg = float(v)
        except (TypeError, ValueError):
            continue
        if cutoff <= d <= today and 25.0 <= kg <= 300.0:
            out.append((d, kg))
    return sorted(out)


def ema(pts: List[Tuple[dt.date, float]], tau_days: float = EMA_TAU_DAYS) -> List[float]:
    """Time-aware exponential moving average: each reading pulls the line by
    ``1 - exp(-Δdays/τ)``, so three weigh-ins in one morning count about as much as one,
    and a reading after a ten-day gap counts for more."""
    out: List[float] = []
    for i, (d, v) in enumerate(pts):
        if i == 0:
            out.append(v)
            continue
        gap = max((d - pts[i - 1][0]).days, 0.5)
        a = 1.0 - math.exp(-gap / tau_days)
        out.append(out[-1] + a * (v - out[-1]))
    return out


def theil_sen_per_week(pts: List[Tuple[dt.date, float]]) -> Optional[float]:
    """Median pairwise slope in kg/week. None when no two points sit on different days."""
    slopes = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            days = (pts[j][0] - pts[i][0]).days
            if days > 0:
                slopes.append((pts[j][1] - pts[i][1]) / days * 7.0)
    if not slopes:
        return None
    slopes.sort()
    n = len(slopes)
    return slopes[n // 2] if n % 2 else (slopes[n // 2 - 1] + slopes[n // 2]) / 2


def trend(history: List[dict], *, today: Optional[dt.date] = None,
          volume_delta_pct: Optional[float] = None,
          target_date: Optional[str] = None) -> Optional[dict]:
    """The whole read, or ``None`` when there aren't :data:`MIN_TREND_POINTS` weigh-ins in
    the window (every consumer then renders nothing — no empty tile).

    ``volume_delta_pct`` is how far this week's training load sits above the athlete's
    usual (NF-20's ``load_forecast.delta_pct``; the caller passes None when it has no
    number) — it is the "on rising volume" half of the second flag. ``target_date`` (the
    active plan's) adds a projection, under the same horizon rule as ``/goal``.
    """
    today = today or dt.date.today()
    pts = points(history, today=today)
    if len(pts) < MIN_TREND_POINTS:
        return None
    smooth = ema(pts)
    current = smooth[-1]

    rate_pts = [p for p in pts if p[0] >= today - dt.timedelta(days=RATE_WINDOW_DAYS - 1)]
    rate = None
    if (len(rate_pts) >= RATE_MIN_POINTS
            and (rate_pts[-1][0] - rate_pts[0][0]).days >= RATE_MIN_SPAN_DAYS):
        rate = theil_sen_per_week(rate_pts)
    rate_pct = rate / current * 100 if rate is not None and current else None

    flags = []
    if rate_pct is not None and rate_pct <= -FAST_LOSS_PCT_WEEK:
        flags.append("fast_loss")
    if (rate_pct is not None and rate_pct <= -LOSS_ON_VOLUME_PCT_WEEK
            and volume_delta_pct is not None and volume_delta_pct >= VOLUME_RISE_PCT):
        flags.append("loss_on_rising_volume")

    projected = weeks_to_target = None
    if target_date and rate is not None:
        try:
            days = (dt.date.fromisoformat(target_date) - today).days
        except (TypeError, ValueError):
            days = None
        if days is not None and 0 <= days / 7.0 <= goal_mod.FAR_HORIZON_WEEKS:
            weeks_to_target = round(days / 7.0, 1)
            projected = round(current + rate * days / 7.0, 1)

    return {
        "current_kg": round(current, 1),
        "latest_kg": round(pts[-1][1], 1),
        "latest_date": pts[-1][0].isoformat(),
        "rate_kg_week": round(rate, 2) if rate is not None else None,
        "rate_pct_week": round(rate_pct, 2) if rate_pct is not None else None,
        "n_points": len(pts),
        "flags": flags,
        "volume_delta_pct": volume_delta_pct,
        "target_date": target_date if projected is not None else None,
        "projected_kg": projected,
        "weeks_to_target": weeks_to_target,
        "series": [(d.isoformat(), round(v, 2)) for (d, _raw), v in zip(pts, smooth)],
    }


_FLAG_TEXT = {
    "fast_loss": "вага знижується швидше за {pct:.1f}% маси тіла на тиждень",
    "loss_on_rising_volume": ("вага знижується ({pct:.1f}%/тиж) на тлі зростання "
                              "тренувального навантаження (+{vol:.0f}% до звичного)"),
}


def flag_lines(t: dict) -> List[str]:
    pct = abs(t.get("rate_pct_week") or 0.0)
    vol = t.get("volume_delta_pct") or 0.0
    return [_FLAG_TEXT[f].format(pct=pct, vol=vol) for f in t.get("flags") or []]


def to_context(t: Optional[dict]) -> Optional[dict]:
    """The daily report's ``body_weight`` block — **only when a flag fired**, otherwise None
    (and absent from the prompt and the cache key alike). Rates and directions only: no
    absolute weight, no series — the analyst needs to know *that* it is falling and how fast,
    not to quote a number on the scale."""
    if not t or not t.get("flags"):
        return None
    return {
        "flags": list(t["flags"]),
        "rate_pct_week": t["rate_pct_week"],
        "volume_delta_pct": t.get("volume_delta_pct"),
        "detail": flag_lines(t),
        "tone": TONE_NOTE,
    }


def summary(t: dict) -> str:
    """Deterministic Ukrainian lines for ``/goal`` — descriptive, no judgement."""
    lines = [f"⚖️ Вага (згладжено): {t['current_kg']:.1f} кг"]
    if t["rate_kg_week"] is None:
        lines.append("Темп зміни: замало зважувань за останні тижні.")
    elif abs(t["rate_kg_week"]) < 0.1:
        lines.append("Темп зміни: стабільно.")
    else:
        lines.append(f"Темп зміни: {t['rate_kg_week']:+.1f} кг/тиж "
                     f"({t['rate_pct_week']:+.1f}% маси).")
    if t["projected_kg"] is not None:
        lines.append(f"Якщо темп збережеться, на {t['target_date']}: ≈{t['projected_kg']:.1f} кг.")
    lines += [f"⚠️ {line[0].upper() + line[1:]}." for line in flag_lines(t)]
    return "\n".join(lines)
