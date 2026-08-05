"""NF-27 · strength: tonnage, estimated 1RM, and closing the progression loop.

**The backlog thought this was blocked by Garmin — it isn't.** BA-AUDIT §5 says "Garmin
doesn't return executed sets/reps; wait for an endpoint", but
``client.fetch_exercise_summary`` has been returning ``{count, reps: [...], weight_kg: [...]}``
per active set, in order, all along — and the prompt even feeds it to Claude as text. The
data was there; the structural work wasn't.

Which left three gaps this module fills:

* ``app.records`` knew only running categories — no strength record of any kind;
* EP-03's progression planned next block's weights **from the model's head**, never checking
  what was actually lifted last week — a textbook open loop;
* tonnage (sets × reps × weight) was computed nowhere, though it's the only strength-volume
  measure comparable between weeks.

Pure: activity rows in, numbers out. The DB read lives in the repository and the narration in
the prompts, same split as ``injury``/``intensity``/``baselines``.

The two rules that keep the numbers honest:

* **Epley (``w × (1 + reps/30)``) is an estimate, not a measurement.** It's fitted for the
  3–10 rep range; above ~12 reps it drifts badly and below 3 it under-reads. So sets over
  :data:`E1RM_MAX_REPS` are ignored entirely, and the reported value is the MEDIAN of the top
  sets rather than the single best — one lucky set is noise, not a personal best.
* **Warm-ups are not attempts.** A set below :data:`WARMUP_FRACTION` of the session's top
  weight for that exercise is dropped before anything is computed.
"""
import datetime as dt
from typing import Dict, List, Optional

from app.statutil import median

# Epley is fitted for low-to-moderate reps; past this it reports fiction with confidence.
E1RM_MAX_REPS = 12
# A set under this share of the day's heaviest for that exercise is a warm-up, not an attempt.
WARMUP_FRACTION = 0.5
# How many of the heaviest qualifying sets the session's e1RM is the median of.
TOP_SETS = 3
# Weeks of flat e1RM at unchanged tonnage before it reads as a stall rather than a plateau week.
STALL_WEEKS = 4
# Below this relative change, an e1RM trend counts as flat.
FLAT_TOLERANCE = 0.02

# The bucket for exercises we have no readable name for (TRX, custom moves, anything Garmin
# couldn't classify). Kept SEPARATE rather than merged into the known ones — mixing an
# unidentified movement into "deadlift" would silently corrupt both its trend and the total.
UNKNOWN_LABEL = "інше"


def _week_of(date_s: Optional[str]) -> Optional[str]:
    try:
        return dt.date.fromisoformat(date_s or "").strftime("%G-W%V")
    except (TypeError, ValueError):
        return None


def _pairs(info: dict):
    """``(reps, weight_kg)`` per set, aligned. Garmin's two lists are per active set and in
    order, but a defensive zip is cheap insurance against a short list from an odd DTO."""
    reps = info.get("reps") or []
    weights = info.get("weight_kg") or []
    return list(zip(reps, weights[:len(reps)] + [None] * max(0, len(reps) - len(weights))))


def session_tonnage(exercises: Optional[dict]) -> Dict[str, float]:
    """``{exercise: kg lifted}`` for one session's stored ``exercises`` blob.

    A bodyweight movement (``weight_kg`` all ``None``) contributes **reps**, not kilograms —
    counting it as zero tonnage would be arithmetically right and analytically useless, and
    multiplying ``None`` would just crash. A timed hold (plank: ``reps`` ``None`` too) is
    neither reps nor kilograms and is skipped entirely rather than fudged into the total."""
    out: Dict[str, float] = {}
    for name, info in ((exercises or {}).get("sets") or {}).items():
        if not isinstance(info, dict):
            continue
        total = 0.0
        for reps, weight in _pairs(info):
            if not isinstance(reps, (int, float)) or reps <= 0:
                continue    # timed hold — no reps to count and no load to multiply
            if isinstance(weight, (int, float)) and weight > 0:
                total += float(reps) * float(weight)
        if total > 0:
            out[name] = round(total, 1)
    return out


def session_reps(exercises: Optional[dict]) -> Dict[str, int]:
    """``{exercise: total reps}`` — the bodyweight-honest volume measure, since a set of
    pull-ups has real volume and zero kilograms."""
    out: Dict[str, int] = {}
    for name, info in ((exercises or {}).get("sets") or {}).items():
        if not isinstance(info, dict):
            continue
        total = sum(int(r) for r, _w in _pairs(info)
                    if isinstance(r, (int, float)) and r > 0)
        if total:
            out[name] = total
    return out


def epley(weight_kg: float, reps: int) -> float:
    """Estimated one-rep max. Always shown as "≈" — see the module docstring."""
    return weight_kg * (1 + reps / 30.0)


def session_e1rm(exercises: Optional[dict]) -> Dict[str, float]:
    """``{exercise: estimated 1RM}`` for one session.

    Warm-ups and high-rep sets are filtered out first, then the value is the MEDIAN of the
    top :data:`TOP_SETS` qualifying estimates — a single lucky set shouldn't become a
    "record" the next block then tries to build on."""
    out: Dict[str, float] = {}
    for name, info in ((exercises or {}).get("sets") or {}).items():
        if not isinstance(info, dict):
            continue
        loaded = [(float(w), int(r)) for r, w in _pairs(info)
                  if isinstance(w, (int, float)) and w > 0
                  and isinstance(r, (int, float)) and 0 < r <= E1RM_MAX_REPS]
        if not loaded:
            continue
        top_weight = max(w for w, _ in loaded)
        working = [(w, r) for w, r in loaded if w >= top_weight * WARMUP_FRACTION]
        if not working:
            continue
        estimates = sorted((epley(w, r) for w, r in working), reverse=True)[:TOP_SETS]
        value = median(estimates)
        if value:
            out[name] = round(value, 1)
    return out


def weekly_stats(activities: List[dict]) -> List[dict]:
    """Per-ISO-week strength aggregates, oldest first:
    ``{week, sessions, tonnage_kg, reps, by_exercise: {name: {tonnage_kg, e1rm}}}``.

    ``activities`` is ``[{date, exercises}]`` — strength rows from the repository. A row with
    no usable ``exercises`` contributes nothing at all (it isn't a zero-tonnage week)."""
    buckets: dict = {}
    for a in activities:
        week = _week_of(a.get("date"))
        ex = a.get("exercises")
        if week is None or not isinstance(ex, dict):
            continue
        tonnage = session_tonnage(ex)
        reps = session_reps(ex)
        e1rms = session_e1rm(ex)
        if not (tonnage or reps):
            continue
        b = buckets.setdefault(week, {"week": week, "sessions": 0, "tonnage_kg": 0.0,
                                      "reps": 0, "by_exercise": {}})
        b["sessions"] += 1
        b["tonnage_kg"] += sum(tonnage.values())
        b["reps"] += sum(reps.values())
        for name in set(tonnage) | set(e1rms):
            slot = b["by_exercise"].setdefault(name, {"tonnage_kg": 0.0, "e1rm": None})
            slot["tonnage_kg"] += tonnage.get(name, 0.0)
            e = e1rms.get(name)
            if e is not None:
                # Best session of the week represents the week for that lift.
                slot["e1rm"] = max(slot["e1rm"] or 0.0, e)

    out = []
    for b in sorted(buckets.values(), key=lambda x: x["week"]):
        b["tonnage_kg"] = round(b["tonnage_kg"], 1)
        for slot in b["by_exercise"].values():
            slot["tonnage_kg"] = round(slot["tonnage_kg"], 1)
        out.append(b)
    return out


def e1rm_trend(weeks: List[dict], exercise: str) -> Optional[dict]:
    """``{exercise, first, last, change_pct, weeks}`` for one lift across the weeks that
    actually have an e1RM for it, or ``None`` with fewer than two such weeks. Gaps are simply
    absent — a week without that lift is not a zero."""
    points = [(w["week"], w["by_exercise"][exercise]["e1rm"])
              for w in weeks
              if exercise in w["by_exercise"] and w["by_exercise"][exercise]["e1rm"]]
    if len(points) < 2:
        return None
    first, last = points[0][1], points[-1][1]
    return {
        "exercise": exercise,
        "first": first,
        "last": last,
        "change_pct": round(100 * (last - first) / first, 1) if first else 0.0,
        "weeks": len(points),
    }


def detect_stalls(weeks: List[dict]) -> List[dict]:
    """Lifts whose e1RM has been flat for :data:`STALL_WEEKS` while tonnage hasn't dropped.

    The tonnage condition is what makes this actionable rather than obvious: flat strength on
    *falling* volume is just a lighter block, but flat strength on steady volume means the
    work is going in and nothing is coming out — that's when the exercise or the rep scheme
    needs changing, not more sets."""
    out: List[dict] = []
    if len(weeks) < STALL_WEEKS:
        return out
    tail = weeks[-STALL_WEEKS:]
    names = set(tail[0]["by_exercise"])
    for name in sorted(names):
        series = [w["by_exercise"].get(name, {}).get("e1rm") for w in tail]
        tonnages = [w["by_exercise"].get(name, {}).get("tonnage_kg") or 0.0 for w in tail]
        if any(v is None for v in series):
            continue
        lo, hi = min(series), max(series)
        if lo <= 0 or (hi - lo) / lo > FLAT_TOLERANCE:
            continue
        if tonnages[-1] < tonnages[0] * 0.9:
            continue    # volume fell — a lighter block explains it
        out.append({
            "exercise": name,
            "weeks": STALL_WEEKS,
            "e1rm": round(series[-1], 1),
            "detail": (f"{name}: ≈1ПМ стоїть на {round(series[-1], 1)} кг "
                       f"{STALL_WEEKS} тижні при незмінному тонажі"),
        })
    return out


def recent_lifts(activities: List[dict], weeks: int = 4) -> Dict[str, dict]:
    """What was ACTUALLY lifted per exercise over the last ``weeks`` — the context that
    closes EP-03's open loop, so the next block starts from the achieved weight instead of a
    number the model invented.

    ``{exercise: {top_weight_kg, typical_reps, e1rm, sessions}}``."""
    stats = weekly_stats(activities)[-weeks:]
    wanted = {w["week"] for w in stats}
    out: Dict[str, dict] = {}
    for a in activities:
        if _week_of(a.get("date")) not in wanted:
            continue
        ex = a.get("exercises")
        if not isinstance(ex, dict):
            continue
        e1rms = session_e1rm(ex)
        for name, info in (ex.get("sets") or {}).items():
            if not isinstance(info, dict):
                continue
            loaded = [(float(w), int(r)) for r, w in _pairs(info)
                      if isinstance(w, (int, float)) and w > 0
                      and isinstance(r, (int, float)) and r > 0]
            slot = out.setdefault(name, {"top_weight_kg": None, "reps": [],
                                         "e1rm": None, "sessions": 0})
            slot["sessions"] += 1
            for w, r in loaded:
                slot["top_weight_kg"] = max(slot["top_weight_kg"] or 0.0, w)
                slot["reps"].append(r)
            e = e1rms.get(name)
            if e is not None:
                slot["e1rm"] = max(slot["e1rm"] or 0.0, e)
    for slot in out.values():
        reps = slot.pop("reps")
        slot["typical_reps"] = int(median(reps)) if reps else None
    return out


def summary(weeks: List[dict], stalls: List[dict]) -> Optional[str]:
    """One deterministic block for ``/records`` and the digest, or ``None`` when there's no
    strength work to talk about."""
    if not weeks:
        return None
    last = weeks[-1]
    lines = [f"🏋️ Силова, {last['week']}: тонаж {last['tonnage_kg']:.0f} кг, "
             f"{last['reps']} повторів за {last['sessions']} сесій"]
    tops = sorted(
        ((n, s["e1rm"]) for n, s in last["by_exercise"].items() if s["e1rm"]),
        key=lambda kv: -kv[1],
    )[:3]
    for name, e in tops:
        lines.append(f"• {name}: ≈1ПМ {e:.0f} кг")
    lines += [f"• ⚠️ {s['detail']}" for s in stalls]
    return "\n".join(lines)


def build_context(weeks: List[dict], stalls: List[dict]) -> dict:
    """Compact context for the digest prompt — only the last few weeks travel."""
    if not weeks:
        return {}
    return {
        "weeks": [
            {"week": w["week"], "sessions": w["sessions"],
             "tonnage_kg": w["tonnage_kg"], "reps": w["reps"],
             "e1rm": {n: s["e1rm"] for n, s in w["by_exercise"].items() if s["e1rm"]}}
            for w in weeks[-4:]
        ],
        "stalls": stalls,
    }
