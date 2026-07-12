"""Subjective check-in aggregation (EP-12 phases 2-3) — pure, zero-LLM.

The MVP stored per-run RPE + pain on ``ActivityRecord.subjective`` and fed it into the
single-activity analysis (phase 1). These are the *cross-run* consumers the ticket
imagined next:

- **plan adaptation** (EP-02) — ease/rebuild when effort trends up for the same pace or a
  niggle recurs (``run_plan_adaptation`` context);
- **daily/morning report** — acknowledge a recurring pain instead of only reacting to the
  objective numbers (``run_analysis`` context);
- **weekly digest** (EP-07) — an "overreached" plan/fact status ("done, but it felt much
  harder than the session called for"), computed in ``repository.weekly_compliance``.

This module only *shapes* the felt-effort signal for those narration prompts. It is kept
separate from :mod:`app.injury` (NF-04), which fuses the same check-ins with load/recovery
into a single severity score for a push advisory — different job, different thresholds.

Input everywhere is the ``recent_subjective_runs`` shape
(``[{date, pace, rpe, pain, note}]``, oldest-first) — only runs that actually got a
check-in (silence is not a signal).
"""
from typing import List, Optional

WINDOW_DAYS = 14        # look back roughly two weeks, matching the injury radar's window
RECENT_N = 6            # how many recent check-ins to hand the LLM verbatim
PAIN_REPEAT = 2         # same body part reported this many times → a recurring niggle
RPE_RISE = 1.5          # avg RPE up by this much (later vs earlier runs) → effort trending up…
PACE_STABLE_PCT = 0.05  # …while typical pace stayed within ±5% (a slower pace would explain it)
MIN_RUNS_FOR_TREND = 4  # need at least this many rated runs to trust an RPE trend
HARD_RPE = 8            # an RPE at/above this felt genuinely hard

# Session types that are *meant* to be easy — a high RPE on one of these is the classic
# "overreached / under-recovered" flag (a hard tempo/interval at RPE 8 is expected, not a flag).
EASY_TYPES = {"easy", "recovery", "base", "long"}


def _norm_part(note: Optional[str]) -> Optional[str]:
    n = (note or "").strip().lower()
    return n or None


def recurring_pain(runs: List[dict]) -> Optional[dict]:
    """The single body part flagged in ``PAIN_REPEAT``+ check-ins in the window, if any —
    ``{part, count}`` for the most-repeated one, else ``None``. A pain with no note counts
    under a generic «біль» bucket."""
    counts: dict = {}
    for r in runs:
        if not r.get("pain"):
            continue
        part = _norm_part(r.get("note")) or "біль"
        counts[part] = counts.get(part, 0) + 1
    repeated = {p: c for p, c in counts.items() if c >= PAIN_REPEAT}
    if not repeated:
        return None
    part, count = max(repeated.items(), key=lambda kv: kv[1])
    return {"part": part, "count": count}


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs)


def rpe_rising(runs: List[dict]) -> bool:
    """True when average RPE rose by ``RPE_RISE``+ from the earlier half of rated runs to the
    later half while typical pace stayed within ``PACE_STABLE_PCT`` — harder effort for the
    same speed, an early fatigue/illness cue (same idea as ``injury._rpe_signal``)."""
    rated = [r for r in runs
             if isinstance(r.get("rpe"), (int, float))
             and isinstance(r.get("pace"), (int, float))]
    if len(rated) < MIN_RUNS_FOR_TREND:
        return False
    mid = len(rated) // 2
    early, late = rated[:mid], rated[mid:]
    if _mean([r["rpe"] for r in late]) - _mean([r["rpe"] for r in early]) < RPE_RISE:
        return False
    pace_early = _mean([r["pace"] for r in early])
    pace_late = _mean([r["pace"] for r in late])
    if pace_early <= 0:
        return False
    return abs(pace_late - pace_early) / pace_early <= PACE_STABLE_PCT


def summarize(runs: List[dict], *, limit: int = RECENT_N) -> Optional[dict]:
    """Compact felt-effort snapshot for the report/adaptation prompts, or ``None`` when there's
    no check-in to speak of. Shape::

        {"n": <checked-in runs in window>,
         "avg_rpe": <mean RPE over rated runs, or None>,
         "rpe_rising": <bool>,
         "recurring_pain": {"part", "count"},   # omitted when none
         "recent": [{"date", "rpe"?, "pain"?, "note"?}, ...]}   # last ``limit``, oldest-first

    Pure and side-effect free — the caller decides where it lands (and adds it to the
    dedup-cache key: the README наскрізна pitfall)."""
    checked = [r for r in runs if r.get("rpe") is not None or r.get("pain")]
    if not checked:
        return None
    rpes = [r["rpe"] for r in checked if isinstance(r.get("rpe"), (int, float))]
    out: dict = {
        "n": len(checked),
        "avg_rpe": round(_mean(rpes), 1) if rpes else None,
        "rpe_rising": rpe_rising(runs),
        "recent": [
            {k: v for k, v in {
                "date": r.get("date"),
                "rpe": r.get("rpe"),
                "pain": r.get("pain") or None,
                "note": _norm_part(r.get("note")),
            }.items() if v is not None}
            for r in checked[-limit:]
        ],
    }
    pain = recurring_pain(runs)
    if pain:
        out["recurring_pain"] = pain
    return out
