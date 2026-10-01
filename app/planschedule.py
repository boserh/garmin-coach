"""The plan's weekly schedule — which weekdays carry a run, which one the long run, which
ones strength — and the pure rules for changing it from chat.

A chat edit used to be able to touch only dated sessions (``PlanOp``), so «3 пробіжки на
тиждень замість 2» became a third session sprinkled into the next few weeks while the plan
itself kept saying 2 days: the open-ended auto-extension rebuilt the next block on the old
days, and /ask kept describing a 2-day plan. A schedule change is a property of the plan,
and changing it means regenerating what is left of the plan (``analysis.plans.
run_plan_rebuild``). This module is the part of that with no I/O: reading the current
schedule off a plan, bounding a proposed one, mapping strength sessions onto new weekdays,
and the one-line description the confirmation shows.
"""
from typing import Optional

from app.daterel import WEEKDAYS_SHORT

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Same floor as the /plan setup form: a plan with one run a week is not a running plan.
MIN_RUN_DAYS = 2


class ScheduleError(ValueError):
    """A proposed schedule that cannot be applied; the message is user-facing."""


def _days(raw) -> list:
    """Valid weekday slugs from ``raw``, de-duplicated, Monday → Sunday."""
    got = {str(d).strip().lower()[:3] for d in (raw or [])}
    return [d for d in WEEKDAYS if d in got]


def strength_days(intake: Optional[dict]) -> list:
    """The weekdays the plan's strength sessions sit on (saved templates and free-text
    sessions alike), or ``[]`` when the plan has no strength."""
    strength = (intake or {}).get("strength") or {}
    if not strength.get("enabled"):
        return []
    return _days(list((strength.get("assignments") or {}).keys())
                 + list((strength.get("custom") or {}).keys()))


def current(plan) -> dict:
    """The plan's schedule as stored: ``{run_days, long_run_day, days_per_week,
    strength_days}``. ``run_days`` is ``[]`` for a plan created before the setup form
    recorded weekdays — the model then has only ``days_per_week`` to go on."""
    intake = plan.intake or {}
    return {
        "run_days": _days(intake.get("run_days")),
        "long_run_day": intake.get("long_run_day"),
        "days_per_week": plan.days_per_week,
        "strength_days": strength_days(intake),
    }


def normalize(proposed, cur: dict) -> dict:
    """Bound a model-proposed schedule (a ``ScheduleOp`` or its dict) against the current
    one. Returns ``{run_days, long_run_day, strength_days}`` where ``strength_days`` is
    ``None`` when strength stays as it is. Raises ``ScheduleError`` when there is nothing
    sane to apply.

    * fewer than ``MIN_RUN_DAYS`` valid run days → error (the setup form's own floor);
    * a long-run day that isn't a run day → the current one if it still is, else the last
      run day of the week (the form's fallback);
    * strength days on a plan WITHOUT strength → ignored: there is no session to put there,
      and inventing one is the setup form's job, not a schedule change's."""
    data = proposed.model_dump() if hasattr(proposed, "model_dump") else dict(proposed or {})
    run_days = _days(data.get("run_days"))
    if len(run_days) < MIN_RUN_DAYS:
        raise ScheduleError(
            f"Потрібно щонайменше {MIN_RUN_DAYS} бігові дні на тиждень.")
    long_day = (data.get("long_run_day") or "").strip().lower()[:3]
    if long_day not in run_days:
        long_day = cur.get("long_run_day") if cur.get("long_run_day") in run_days \
            else run_days[-1]
    new_strength = None
    if data.get("strength_days") is not None and cur.get("strength_days"):
        new_strength = _days(data.get("strength_days"))
        if new_strength == cur["strength_days"]:
            new_strength = None
    return {"run_days": run_days, "long_run_day": long_day,
            "strength_days": new_strength}


def is_change(new: dict, cur: dict) -> bool:
    """True when ``new`` (a ``normalize`` result) differs from the plan's schedule."""
    return (new["run_days"] != cur.get("run_days")
            or new["long_run_day"] != cur.get("long_run_day")
            or new.get("strength_days") is not None)


def remap(old_days: list, new_days: list) -> dict:
    """``{new weekday: old weekday}`` — which existing strength session each new strength
    day takes. A day that stays a strength day keeps its own session; the other new days
    take the sessions left over, in weekday order, then cycle through all of them when
    there are more new days than old ones (Day 1/Day 2 keep alternating). Sessions with no
    day left are dropped. Empty when either side is empty."""
    old = _days(old_days)
    new = _days(new_days)
    if not old:
        return {}
    out = {d: d for d in new if d in old}
    spare = [d for d in old if d not in out]
    for i, d in enumerate(d for d in new if d not in out):
        out[d] = spare[i] if i < len(spare) else old[(i - len(spare)) % len(old)]
    return {d: out[d] for d in new}


def remap_strength_intake(strength: dict, new_days: list) -> dict:
    """The plan's ``intake["strength"]`` with its weekday-keyed pieces moved onto
    ``new_days`` (per ``remap``), so a later extension lays strength on the NEW days.
    No days left → strength switched off."""
    mapping = remap(strength_days({"strength": strength}), new_days)
    if not mapping:
        return {"enabled": False}
    out = {"enabled": True}
    for key in ("assignments", "custom", "custom_generated"):
        src = strength.get(key) or {}
        moved = {new: src[old] for new, old in mapping.items() if old in src}
        if moved:
            out[key] = moved
    return out


def day_list(days: list) -> str:
    """``["tue", "thu", "sun"]`` → ``"вт, чт, нд"``."""
    return ", ".join(WEEKDAYS_SHORT[WEEKDAYS.index(d)] for d in _days(days))


def _days_word(n: int) -> str:
    return "дні" if n in (2, 3, 4) else "днів"


def describe(new: dict, cur: dict) -> list:
    """Human lines for the confirmation, old → new. Ukrainian, like the rest of the chat."""
    n_new = len(new["run_days"])
    n_old = len(cur.get("run_days") or []) or cur.get("days_per_week")
    lead = (f"біг: {n_old} → {n_new} {_days_word(n_new)} на тиждень"
            if n_old and n_old != n_new else
            f"біг: {n_new} {_days_word(n_new)} на тиждень")
    lines = [f"{lead} ({day_list(new['run_days'])}; довгий — "
             f"{day_list([new['long_run_day']])})"]
    if new.get("strength_days") is not None:
        old_s = cur.get("strength_days") or []
        if new["strength_days"]:
            lines.append(f"силові: {len(old_s)} → {len(new['strength_days'])} "
                         f"({day_list(new['strength_days'])})")
        else:
            lines.append("силові: прибрати з програми")
    return lines


def confirmation_lines(proposal: dict) -> list:
    """Every line a schedule-change confirmation shows before ✅ — the change itself, what
    happens to the plan, and the price — shared by the bot and the web chat so the two
    can't promise different things. ``proposal`` is ``analysis.plans.schedule_proposal``'s
    dict."""
    lines = list(proposal.get("lines") or [])
    start = proposal.get("start") or ""
    when = f"з {start[8:10]}.{start[5:7]}" if len(start) == 10 else "із завтра"
    lines.append(f"{when}: решту програми буде згенеровано заново під новий розклад "
                 f"(і замінено в календарі Garmin); минулі тренування лишаються як є")
    cost = proposal.get("cost_usd")
    lines.append("генерація: 1–2 хв" + (f", до ~${cost:.2f}" if cost else ""))
    return lines
