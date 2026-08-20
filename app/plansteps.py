"""Keep a planned session's headline ``dist_km`` and its structured ``steps`` telling the
same story — pure functions, no DB, no I/O.

The two live in separate columns and used to be written independently: an adaptation that
softens a session ("зменшую завтрашній лонг з 6 до 5 км") legitimately returns a `modify`
with `dist_km` and a new `description` but no `steps`, so the row ended up claiming 5.0 km
in the header while its only step still said 6000 m. That is worse than a cosmetic mismatch:
``workout_export`` builds the watch workout from ``steps``, so the easing existed on screen
only — the run pushed to Garmin was the original one.

Rules here:

* ``total_dist_m`` — a workout's distance as the steps actually describe it (``repeat``
  blocks counted ``reps`` times). ``None`` when no step carries a distance (a purely
  time-based session), which is the "nothing to reconcile" signal, not zero.
* ``describes_distance`` — whether that sum is the WHOLE session. A step measured in time
  ("5×2 хв") covers real ground this module cannot know, so a session mixing distance and
  time steps is only PARTLY described in metres and nothing here may be derived from the
  sum. Getting this wrong is not academic: "розминка 1.5 км + 5×2 хв + заминка 1.5 км"
  totals 3000 m of *distance steps* against a perfectly correct 6.0 km headline, and the
  reconcile below used to overwrite the headline with 3.0 — turning a right number into a
  wrong one, and warning about a prompt regression that never happened.
* ``scale_steps`` — re-cut the steps to a new total. Work steps (``run``) absorb the change
  and warmup/cooldown/recovery keep their prescribed length, because a coach who cuts volume
  cuts the work, not the warmup; only when the fixed parts alone already exceed the target
  does everything scale proportionally.
* ``reconcile`` — the one decision table both writers use.
"""

from __future__ import annotations

from typing import List, Optional, TypeGuard

# Below this relative difference the two numbers are "the same" — steps rounded to 10 m
# against a 0.1 km headline can never be exactly equal, and rewriting steps over that is
# churn.
TOLERANCE = 0.02

# Steps whose distance is the session's WORK — these absorb a volume change first.
_WORK_KINDS = ("run", "ride")

# Round every rescaled step to this many metres. Sub-km steps (50 m strides, 100 m
# recoveries) stay legible instead of turning into 47 m.
_ROUND_M = 10.0


def _is_num(v) -> TypeGuard[float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def total_dist_m(steps) -> Optional[float]:
    """Total metres described by ``steps`` (``repeat`` blocks × their ``reps``), or None
    when no step carries a distance at all."""
    if not steps:
        return None
    total = 0.0
    seen = False
    for s in steps:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "repeat":
            inner = total_dist_m(s.get("steps"))
            if inner is not None:
                total += inner * max(int(s.get("reps") or 1), 1)
                seen = True
            continue
        dm = s.get("dist_m")
        if _is_num(dm):
            total += float(dm)
            seen = True
    return total if seen else None


def _is_timed(s: dict) -> bool:
    """A leaf step prescribed in TIME with no distance — it covers metres we cannot know."""
    return not _is_num(s.get("dist_m")) and _is_num(s.get("dur_s"))


def has_timed_steps(steps) -> bool:
    """True when any step (at any depth) is prescribed in time rather than distance."""
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "repeat":
            if has_timed_steps(s.get("steps")):
                return True
        elif _is_timed(s):
            return True
    return False


def describes_distance(steps) -> bool:
    """True when ``total_dist_m`` is the session's WHOLE distance — i.e. some step carries
    one and none is timed. Only then may the headline be compared to, derived from, or
    rescaled against the steps; a mixed session's metres are simply unknown here."""
    return total_dist_m(steps) is not None and not has_timed_steps(steps)


def _work_dist_m(steps) -> float:
    """Metres sitting in work steps (``repeat`` blocks counted in full)."""
    total = 0.0
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "repeat":
            inner = _work_dist_m(s.get("steps"))
            total += inner * max(int(s.get("reps") or 1), 1)
        else:
            dm = s.get("dist_m")
            if s.get("kind") in _WORK_KINDS and _is_num(dm):
                total += float(dm)
    return total


def _scale_all(steps, factor: float) -> list:
    """Multiply every distance in the tree by ``factor``."""
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            out.append(s)
            continue
        s2 = dict(s)
        dm = s2.get("dist_m")
        if s2.get("kind") == "repeat":
            s2["steps"] = _scale_all(s2.get("steps"), factor)
        elif _is_num(dm):
            s2["dist_m"] = max(round(float(dm) * factor / _ROUND_M) * _ROUND_M, _ROUND_M)
        out.append(s2)
    return out


def _scale_work(steps, factor: float) -> list:
    """Multiply only work-step distances by ``factor``; warmup/cooldown/recovery keep theirs."""
    out = []
    for s in steps or []:
        if not isinstance(s, dict):
            out.append(s)
            continue
        s2 = dict(s)
        dm = s2.get("dist_m")
        if s2.get("kind") == "repeat":
            s2["steps"] = _scale_work(s2.get("steps"), factor)
        elif s2.get("kind") in _WORK_KINDS and _is_num(dm):
            s2["dist_m"] = max(round(float(dm) * factor / _ROUND_M) * _ROUND_M, _ROUND_M)
        out.append(s2)
    return out


def _largest_step(steps) -> Optional[dict]:
    """The single distance step holding the most metres — where a rounding residue goes.
    Prefers work steps; a step inside a ``repeat`` is skipped (nudging it would move
    ``reps`` × the residue)."""
    best, best_m = None, 0.0
    for pref in (_WORK_KINDS, None):
        for s in steps or []:
            if not isinstance(s, dict) or s.get("kind") == "repeat":
                continue
            if pref is not None and s.get("kind") not in pref:
                continue
            dm = s.get("dist_m")
            if _is_num(dm) and float(dm) > best_m:
                best, best_m = s, float(dm)
        if best is not None:
            return best
    return best


def scale_steps(steps, target_km: float) -> Optional[list]:
    """Re-cut ``steps`` so they total ``target_km``, or None when they can't be rescaled
    (no distances, a non-positive target, or nothing to change).

    Work steps take the change; the warmup/cooldown stay as prescribed. If the fixed parts
    alone already cover the target, everything scales proportionally instead — a 2 km target
    cannot keep a 1.5 km warmup plus a 1.5 km cooldown intact.
    """
    if not steps or not _is_num(target_km) or target_km <= 0:
        return None
    if has_timed_steps(steps):
        # Part of the session's distance sits in timed steps, so hitting ``target_km`` by
        # scaling the rest would push the whole change onto the warmup/cooldown — the exact
        # opposite of the rule above. Better to leave the session alone than to re-cut it wrong.
        return None
    current = total_dist_m(steps)
    if current is None or current <= 0:
        return None
    target_m = float(target_km) * 1000.0
    if abs(current - target_m) / current <= TOLERANCE:
        return None

    work = _work_dist_m(steps)
    fixed = current - work
    wanted_work = target_m - fixed
    if work > 0 and wanted_work >= _ROUND_M:
        out = _scale_work(steps, wanted_work / work)
    else:
        out = _scale_all(steps, target_m / current)

    # Rounding leaves up to a few metres on the table; put the residue on the biggest step
    # so the total matches the headline exactly.
    got = total_dist_m(out) or 0.0
    residue = target_m - got
    if abs(residue) >= 1.0:
        big = _largest_step(out)
        if big is not None:
            nudged = float(big["dist_m"]) + residue
            if nudged >= _ROUND_M:
                big["dist_m"] = nudged
    return out


def mismatch(dist_km: Optional[float], steps) -> Optional[float]:
    """Relative gap between the headline distance and what the steps describe (0.17 = the
    steps say 17% more than the header), or None when there is nothing to compare."""
    if not _is_num(dist_km) or dist_km <= 0:
        return None
    if not describes_distance(steps):
        return None
    total = total_dist_m(steps)
    if total <= 0:
        return None
    return abs(total - float(dist_km) * 1000.0) / (float(dist_km) * 1000.0)


def reconcile(dist_km: Optional[float], steps, *, steps_given: bool):
    """Return the ``(dist_km, steps)`` pair to actually store.

    ``steps_given`` says whether the steps come from the SAME write as ``dist_km`` (a fresh
    generation, or an edit that sent both) or are the row's existing ones:

    * steps written alongside the distance — the steps win and ``dist_km`` is set to their
      total. They are what reaches the watch, so they define the session.
    * a new distance over pre-existing steps — the distance is the intent (that is what the
      coach just decided) and the stale steps are re-cut to match it.
    """
    if not describes_distance(steps):
        return dist_km, steps          # purely or partly timed — the metres are not ours to judge
    total = total_dist_m(steps)
    if total <= 0:
        return dist_km, steps
    if not _is_num(dist_km) or dist_km <= 0:
        return round(total / 1000.0, 2), steps
    if abs(total - float(dist_km) * 1000.0) / total <= TOLERANCE:
        return dist_km, steps
    if steps_given:
        return round(total / 1000.0, 2), steps
    rescaled = scale_steps(steps, dist_km)
    return dist_km, (rescaled if rescaled is not None else steps)


__all__: List[str] = ["TOLERANCE", "total_dist_m", "has_timed_steps",
                      "describes_distance", "scale_steps", "mismatch", "reconcile"]
