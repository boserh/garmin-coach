"""EP-05 · Race pack — pre-race pacing/fueling/checklist synthesis.

Every ingredient already exists elsewhere: race-time predictions/VO2max/endurance in
``DailyMetric.extra`` (see ``app.goal``), the target date/distance implied by a plan's
``goal`` (``TrainingPlan.target_date`` is already a typed ISO string — see
``app.db.models.TrainingPlan``), weather via ``app.weather``, and the taper itself is
already baked into the generated plan's last sessions. What was missing (EP-05 "phase 0")
was a typed **target distance**: ``TrainingPlan.goal`` names a race ("first_10k") but
nothing mapped it to a km number a pacing calc can use — that's :data:`GOAL_DISTANCE_KM`
below, the sibling of ``app.goal.GOAL_METRIC`` (which maps a goal to the Garmin
*prediction* metric, not a fixed distance).

Pure Python, zero LLM, zero network (mirrors ``compare.py``/``wrapped.py``/``goal.py``'s
shape): this module only decides WHETHER a plan has a race pack to give and assembles the
narration context; all pacing/fueling numbers are Claude's job (``SYSTEM_RACE``, Opus) —
it forwards what Garmin/the plan already computed, never invents its own.
"""
import datetime as dt
from typing import Optional

from app import goal as goal_mod

# Auto-send the race pack exactly this many days before target_date (bot/jobs.py's daily
# plan_sync_job checks this once a day — the guard is per-plan, not per-date, so a missed
# tick doesn't lose the trigger, but it also never fires twice for the same plan).
TRIGGER_DAYS = 7

# NF-22: race-week countdown — two deterministic (zero-LLM) follow-ups on top of the
# T-7 narrated pack above. STAGE_PACK is just TRIGGER_DAYS under its stage name.
STAGE_PACK = TRIGGER_DAYS       # T-7: the narrated pack (existing EP-05 behaviour)
STAGE_CHECKLIST = 3             # T-3: deterministic prep checklist
STAGE_BRIEF = 1                 # T-1 evening: final weather + pace brief (catches up
                                 # through race day itself if the T-1 tick was missed)

# Only fold a forecast into the pack when the race is this close — Open-Meteo's daily
# forecast is unreliable much further out (same reasoning as EP-13's decision window).
WEATHER_WINDOW_DAYS = 7

# /plan shows the last generated pack as a standing block while the race is this close.
PLAN_BLOCK_DAYS = 14

# plan.goal -> target race distance, km. Deliberately separate from goal.GOAL_METRIC
# (which maps to the *prediction metric* Garmin tracks, not a fixed distance) — the
# open-ended "general" goal has neither a distance nor a race date, so it has no pack.
GOAL_DISTANCE_KM = {
    "first_5k": 5.0,
    "faster_5k": 5.0,
    "first_10k": 10.0,
    "first_half": 21.0975,
}


def distance_for_goal(goal: Optional[str]) -> Optional[float]:
    """This goal's race distance in km, or None (open-ended/unrecognised goals)."""
    return GOAL_DISTANCE_KM.get(goal or "")


def has_target(plan) -> bool:
    """True when a plan carries both a race date and a distance we can pace — the two
    things a race pack needs. An open-ended (``general``) plan, or no plan at all, has
    neither."""
    return bool(plan and plan.target_date and distance_for_goal(plan.goal))


def days_to_target(target_date: Optional[str], today: Optional[dt.date] = None) -> Optional[int]:
    """Whole days from ``today`` to ``target_date`` (may be negative for a past date), or
    None when ``target_date`` is missing/unparsable."""
    if not target_date:
        return None
    try:
        return (dt.date.fromisoformat(target_date) - (today or dt.date.today())).days
    except ValueError:
        return None


def build_context(plan, fitness: Optional[dict], recent_sessions: list,
                   forecast_day: Optional[dict]) -> dict:
    """Assemble the narration context for :func:`app.analysis.reports.run_race_plan`.
    ``recent_sessions`` are the plan's own upcoming sessions through race day (its taper —
    the model is told to reference them, not invent a different one); ``forecast_day`` is
    the target date's forecast row (only present within :data:`WEATHER_WINDOW_DAYS`)."""
    metric_key, _label, _higher_better = goal_mod.metric_for_goal(plan.goal)
    return {
        "goal": plan.goal,
        "goal_label": plan.goal_label,
        "target_date": plan.target_date,
        "target_dist_km": distance_for_goal(plan.goal),
        "target_metric": metric_key,
        "days_left": days_to_target(plan.target_date),
        "fitness": fitness,
        "recent_sessions": recent_sessions,
        "weather": forecast_day,
    }


def stage_for(days_left: Optional[int]) -> Optional[str]:
    """NF-22: which race-week stage (if any) is due today, given ``days_left`` days to
    the target date. ``"pack"`` fires exactly at :data:`STAGE_PACK` (unchanged EP-05
    behaviour — bot/jobs.py's own guard on that stage predates this function).
    ``"checklist"``/``"brief"`` each get a 2-day catch-up window ending on their nominal
    day, so a single missed tick (e.g. the Pi was down) doesn't silently drop a stage —
    the per-(plan, stage) guard in bot/jobs.py still makes each fire at most once, no
    matter which day inside its window it actually catches on."""
    if days_left is None or days_left < 0:
        return None
    if days_left == STAGE_PACK:
        return "pack"
    if STAGE_CHECKLIST - 1 <= days_left <= STAGE_CHECKLIST:
        return "checklist"
    if STAGE_BRIEF - 1 <= days_left <= STAGE_BRIEF:
        return "brief"
    return None


def _weather_line(forecast_day: Optional[dict]) -> Optional[str]:
    if not forecast_day:
        return None
    feels = forecast_day.get("feels_max_c")
    if feels is None:
        return None
    bits = [f"відчувається як {feels:.0f}°C"]
    precip = forecast_day.get("precip_mm")
    if precip:
        bits.append(f"опади {precip:.0f} мм")
    wind = forecast_day.get("wind_max_kmh")
    if wind:
        bits.append(f"вітер {wind:.0f} км/год")
    return ", ".join(bits)


def checklist_text(plan, forecast_day: Optional[dict]) -> str:
    """NF-22 T-3: a deterministic (zero-LLM) prep checklist — gear, fuelling, logistics —
    plus the forecast for race day when one's already fetchable (within Open-Meteo's
    reliable window by then)."""
    lines = [
        f"🎽 3 дні до старту ({plan.target_date})! Час підготуватись:",
        "• Виклади форму, взуття, номер, гелі/бутилку сьогодні — не в останню ніч.",
        "• Почни вуглеводну підготовку останніх днів.",
        "• Перевір маршрут і час виїзду до старту.",
        "• Лишились лише легкі сесії — без нових експериментів у взутті чи харчуванні.",
    ]
    weather_line = _weather_line(forecast_day)
    if weather_line:
        lines.append(f"• Прогноз на день старту: {weather_line}.")
    return "\n".join(lines)


def brief_text(
    plan, forecast_day: Optional[dict], pack_text: Optional[str],
    bedtime: Optional[str] = None, days_left: Optional[int] = None,
) -> str:
    """NF-22 T-1: a deterministic evening brief — final weather, an early-bedtime
    reminder (a concrete time when NF-21's ``recommended_bedtime`` has enough data,
    a generic nudge otherwise), and the saved race pack quoted back (not
    re-parsed/re-generated — a fresh Claude call here would break the cost rules) so the
    target/backup pace is one scroll away instead of a fragile regex extraction. Missing
    pack (never generated, or generation failed) degrades to weather + logistics only."""
    heading = "🌙 Сьогодні старт!" if days_left == 0 else "🌙 Завтра старт!"
    lines = [f"{heading} Останній чекліст:"]
    weather_line = _weather_line(forecast_day)
    if weather_line:
        lines.append(f"• Погода на старт: {weather_line}.")
    if bedtime:
        lines.append(f"• Лягай сьогодні до {bedtime} — виспатись важливіше за останню пробіжку.")
    else:
        lines.append("• Лягай раніше — виспатись важливіше за останню пробіжку.")
    if pack_text:
        lines.append("\nТвій pack з темпом/фінальним планом:\n" + pack_text)
    else:
        lines.append("\nPack не генерувався — біжи за відчуттями і планом тренера.")
    return "\n".join(lines)
