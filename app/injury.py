"""Injury-risk radar (NF-04) — a pure-Python, zero-LLM signal detector.

ACWR is already in the plan context, but nobody looks at the **combination** of early
warning signs. This is the load-side detector the backlog imagined sitting next to EP-08:
it fuses four cheap signals already in the DB into one severity score, so a run of quietly
accumulating risk earns a single heads-up instead of a surprise injury.

Signals (each contributes a severity; the strongest — subjective pain — weighs most):
  1. **ACWR sustained high** — acute:chronic load ratio ≥ threshold on several recent days
     (`daily_metrics.extra.acwr_pct`).
  2. **Repeated pain** — the same body part flagged ≥2× in the window (EP-12 check-ins,
     `activities.subjective`). The single best predictor, so it weighs heaviest.
  3. **RPE rising at a stable pace** — the run felt harder for the same speed (early fatigue
     / illness), from the EP-12 RPE + the run's pace.
  4. **Recovery drift** — HRV below its baseline band for several days and/or resting HR
     drifting up.

Calibration (the EP-08 pitfall: false positives kill trust): **no warnings until the user
has ``min_history_days`` of data** — the detector runs in a quiet mode first, exactly like
records/baselines gate their announcements. Pure and side-effect free; the caller fetches the
windowed inputs and decides what to do with the assessment (a morning advisory, the ``/risk``
command). The advisory *text* is optionally narrated by Sonnet, but :func:`summary` gives a
deterministic fallback so the detector never depends on an LLM to warn.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional

WINDOW_DAYS = 14          # signals look at roughly the last two weeks

# ACWR (acute:chronic load, %). 80–130 is the optimal band; sustained above HIGH is the
# classic load-spike injury flag.
ACWR_HIGH = 140.0
ACWR_VERY_HIGH = 160.0
ACWR_MIN_READINGS = 3     # this many high readings in the window = "sustained", not a blip

PAIN_REPEAT = 2           # same body part reported this many times → a recurring niggle

RPE_RISE = 2.0            # avg RPE up by this much (later runs vs earlier)…
PACE_STABLE_PCT = 0.05    # …while typical pace stayed within ±5% → effort/fatigue divergence
MIN_RUNS_FOR_RPE = 4      # need at least this many rated runs to trust an RPE trend

HRV_LOW_DAYS = 3          # HRV below its baseline band for this many days → undue fatigue
RHR_DRIFT = 5.0           # resting HR this many bpm over the window median → drift up

# Severity → risk level. Pain alone (severity 3) already clears "elevated"; pain + any other
# signal (≥5) or two strong load signals reach "high" and earn an advisory.
LEVEL_ELEVATED = 2
LEVEL_HIGH = 5


@dataclass
class Signal:
    kind: str          # acwr / pain / rpe / recovery
    severity: int
    detail: str        # a human, Ukrainian one-liner


@dataclass
class Assessment:
    level: str                       # calibrating / none / elevated / high
    score: int = 0
    signals: List[Signal] = field(default_factory=list)
    history_days: int = 0

    @property
    def actionable(self) -> bool:
        """True when there's a real, non-calibration warning worth surfacing."""
        return self.level in ("elevated", "high")


def _level_from_score(score: int) -> str:
    if score >= LEVEL_HIGH:
        return "high"
    if score >= LEVEL_ELEVATED:
        return "elevated"
    return "none"


def _acwr_signal(daily: List[dict]) -> Optional[Signal]:
    vals = [float(r["acwr_pct"]) for r in daily
            if isinstance(r.get("acwr_pct"), (int, float))]
    if not vals:
        return None
    high = [v for v in vals if v >= ACWR_HIGH]
    if len(high) < ACWR_MIN_READINGS:
        return None
    peak = max(high)
    sev = 3 if peak >= ACWR_VERY_HIGH else 2
    return Signal(
        "acwr", sev,
        f"Навантаження (ACWR) кілька днів високе — пік {round(peak)}% "
        f"(норма до ~130%): гостра втома росте швидше за форму.",
    )


def _norm_part(note: Optional[str]) -> Optional[str]:
    n = (note or "").strip().lower()
    return n or None


def _pain_signal(runs: List[dict]) -> Optional[Signal]:
    counts: dict = {}
    for r in runs:
        if not r.get("pain"):
            continue
        part = _norm_part(r.get("note")) or "біль"
        counts[part] = counts.get(part, 0) + 1
    repeated = {p: c for p, c in counts.items() if c >= PAIN_REPEAT}
    if not repeated:
        return None
    part, c = max(repeated.items(), key=lambda kv: kv[1])
    # Two-plus distinct pains, or one part many times — both strong; weight heaviest.
    sev = 4 if (c >= 3 or len(repeated) >= 2) else 3
    return Signal(
        "pain", sev,
        f"Той самий біль повторюється: «{part}» ≥{c}× за два тижні. "
        f"Повторюваний біль — найсильніший ранній сигнал травми.",
    )


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs)


def _rpe_signal(runs: List[dict]) -> Optional[Signal]:
    rated = [r for r in runs
             if isinstance(r.get("rpe"), (int, float))
             and isinstance(r.get("pace"), (int, float))]
    if len(rated) < MIN_RUNS_FOR_RPE:
        return None
    mid = len(rated) // 2
    early, late = rated[:mid], rated[mid:]
    rpe_early, rpe_late = _mean([r["rpe"] for r in early]), _mean([r["rpe"] for r in late])
    pace_early, pace_late = _mean([r["pace"] for r in early]), _mean([r["pace"] for r in late])
    if rpe_late - rpe_early < RPE_RISE:
        return None
    # Pace "stable" = not meaningfully easier (a slower pace would explain higher RPE away).
    if pace_early <= 0 or abs(pace_late - pace_early) / pace_early > PACE_STABLE_PCT:
        return None
    return Signal(
        "rpe", 2,
        f"Пробіжки відчуваються важче за той самий темп "
        f"(RPE {rpe_early:.1f}→{rpe_late:.1f}) — ранній сигнал втоми чи застуди.",
    )


def _recovery_signal(daily: List[dict]) -> Optional[Signal]:
    low_hrv = sum(
        1 for r in daily
        if isinstance(r.get("hrv_avg"), (int, float))
        and isinstance(r.get("hrv_baseline_low"), (int, float))
        and r["hrv_avg"] < r["hrv_baseline_low"]
    )
    rhr = [float(r["resting_hr"]) for r in daily
           if isinstance(r.get("resting_hr"), (int, float))]
    drift = False
    if len(rhr) >= 3:
        s = sorted(rhr)
        median = s[len(s) // 2]
        drift = rhr[-1] >= median + RHR_DRIFT

    parts = []
    sev = 0
    if low_hrv >= HRV_LOW_DAYS:
        sev += 2
        parts.append(f"HRV нижче базової смуги {low_hrv} дн.")
    if drift:
        sev += 1
        parts.append(f"пульс спокою дрейфує вгору (+{RHR_DRIFT:.0f})")
    if sev == 0:
        return None
    return Signal("recovery", sev, "Відновлення просідає: " + ", ".join(parts) + ".")


def assess(
    daily: List[dict], runs: List[dict], *, history_days: int,
    min_history_days: int = 14, today: Optional[dt.date] = None,
) -> Assessment:
    """Fuse the windowed signals into an :class:`Assessment`. ``daily`` (recovery/load rows,
    each ``{date, hrv_avg, resting_hr, acwr_pct, hrv_baseline_low}``) and ``runs``
    (``{date, pace, rpe, pain, note}``), both oldest-first, are the last ~``WINDOW_DAYS``.
    ``history_days`` is the user's TOTAL days of data — under ``min_history_days`` we stay in
    a quiet calibration mode (``level="calibrating"``) and raise no warning (the EP-08 anti-
    false-positive rule). Pure; ``today`` is accepted for symmetry/testing but unused here."""
    if history_days < min_history_days:
        return Assessment(level="calibrating", history_days=history_days)

    signals = [s for s in (
        _pain_signal(runs),
        _acwr_signal(daily),
        _recovery_signal(daily),
        _rpe_signal(runs),
    ) if s is not None]
    signals.sort(key=lambda s: s.severity, reverse=True)
    score = sum(s.severity for s in signals)
    return Assessment(
        level=_level_from_score(score), score=score,
        signals=signals, history_days=history_days,
    )


# ---------- FORMATTING ----------

_HEAD = {
    "high": "🔴 Підвищений ризик травми",
    "elevated": "🟠 Кілька сигналів ризику",
}


def summary(a: Assessment) -> str:
    """A deterministic advisory for an actionable assessment — used by ``/risk`` and as the
    LLM-free fallback for the morning warning. Conservative, non-medical: it flags signals and
    suggests easing / a deload via the plan, never diagnoses."""
    head = _HEAD.get(a.level, "Сигнали ризику")
    lines = [head, ""]
    lines += [f"• {s.detail}" for s in a.signals]
    lines.append("")
    lines.append(
        "Порада: наступні кілька днів прибери інтенсивність, додай легкого/відпочинку. "
        "Можеш попросити перебудову плану через /plan. Якщо біль не вщухає — до лікаря."
    )
    return "\n".join(lines)


def to_context(a: Assessment) -> dict:
    """Compact dict of the signals for the Claude narration context (and any cache key)."""
    return {
        "level": a.level,
        "score": a.score,
        "signals": [{"kind": s.kind, "severity": s.severity, "detail": s.detail}
                    for s in a.signals],
    }
