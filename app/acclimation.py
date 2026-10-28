"""NF-31 · Heat & altitude acclimation — "hot" vs "hot *for you, right now*".

Every heat rule in the app looked only at today's weather: 28 °C feels-like meant "drink and
slow down" for everyone. But 28 °C after two weeks of running in a heatwave and 28 °C the day
after flying in from 15 °C are different sessions. Garmin already estimates both
acclimations (``heatAcclimationPercentage`` / ``altitudeAcclimation`` in the training-status
DTO, stored by ``app.garmin.service`` as ``heat_acclimation_pct`` / ``altitude_acclimation_m``
in ``DailyMetric.extra``) — nothing read them.

Garmin documents no formula, so these are used as a **relative** signal only: a class (none /
partial / acclimated) and its direction, never a coefficient pretending to precision. The one
number this module does produce — the heat pace adjustment — comes from a fixed table in code
(:data:`HEAT_PACE_PCT`, :data:`ACCLIMATION_FACTOR`), so it can't drift between two calls the
way an LLM's improvised "+10 s/km" would.

Sources for the table (orders of magnitude, rounded to whole percents):

* Ely MR, Cheuvront SN, Roberts WO, Montain SJ. *Impact of weather on marathon-running
  performance.* Med Sci Sports Exerc 2007;39(3):487–493 — finishing times slow progressively
  as conditions warm past ~10–15 °C, by a few percent at 20–25 °C and markedly beyond.
* Racinais S et al. *Consensus recommendations on training and competing in the heat.*
  Scand J Med Sci Sports 2015;25(S1):6–19 — 1–2 weeks of heat exposure recovers a large part
  of the heat decrement (most adaptations in 5–7 days, near-complete in ~14); adaptation
  decays over weeks without exposure.

Pure, zero LLM, zero network. Every consumer degrades to silence when the watch reports
neither field — the most likely case on older models.
"""
import datetime as dt
from typing import List, Optional, Tuple

HEAT_KEY = "heat_acclimation_pct"
ALT_KEY = "altitude_acclimation_m"
EXTRA_KEYS = (HEAT_KEY, ALT_KEY, "heat_trend", "altitude_trend", "training_status",
              "load_balance")

# Heat acclimation % → class.
HEAT_PARTIAL_PCT = 30
HEAT_ACCLIMATED_PCT = 70
# Altitude acclimation (metres Garmin considers you adapted to) → class.
ALT_PARTIAL_M = 800
ALT_ACCLIMATED_M = 2000

LOSS_WINDOW_DAYS = 7
HEAT_LOSS_PP = 20          # heat acclimation down this many points in a week → "lost"
ALT_LOSS_M = 500           # altitude acclimation down this many metres in a week → "lost"

CLASS_LABEL = {"none": "не адаптований", "partial": "частково адаптований",
               "acclimated": "адаптований"}

# Race-day feels-like °C (lower bound of the band) → % slower than the cool-weather target
# pace for an UNACCLIMATED runner. See the module docstring for the sources.
HEAT_PACE_PCT: List[Tuple[float, float]] = [
    (15.0, 1.0), (20.0, 3.0), (25.0, 5.0), (30.0, 8.0),
]
# How much of that decrement still applies, by acclimation class.
ACCLIMATION_FACTOR = {"none": 1.0, "partial": 0.75, "acclimated": 0.5}

# NF-11: an unacclimated runner sweats less efficiently and gets dehydrated sooner on a hot day.
FLUID_ML_H_HOT_UNACCLIMATED = 900


def heat_class(pct) -> Optional[str]:
    if not isinstance(pct, (int, float)):
        return None
    if pct >= HEAT_ACCLIMATED_PCT:
        return "acclimated"
    return "partial" if pct >= HEAT_PARTIAL_PCT else "none"


def altitude_class(metres) -> Optional[str]:
    if not isinstance(metres, (int, float)):
        return None
    if metres >= ALT_ACCLIMATED_M:
        return "acclimated"
    return "partial" if metres >= ALT_PARTIAL_M else "none"


def _latest(history: List[dict], key: str, *, on_or_before: Optional[str] = None):
    for row in reversed(history or []):
        if on_or_before and (row.get("date") or "") > on_or_before:
            continue
        v = (row.get("extra") or {}).get(key)
        if isinstance(v, (int, float)):
            return v, row.get("date")
    return None, None


def status(history: List[dict], *, today: Optional[dt.date] = None) -> Optional[dict]:
    """The current read from ``read_history``-shaped rows (oldest first), or ``None`` when
    neither field was ever reported. ``lost_heat``/``lost_altitude`` mark a drop over the
    last :data:`LOSS_WINDOW_DAYS` big enough to explain "I suddenly got slower" (back from
    the mountains, or a hot spell that ended)."""
    today = today or dt.date.today()
    heat, heat_date = _latest(history, HEAT_KEY)
    alt, alt_date = _latest(history, ALT_KEY)
    if heat is None and alt is None:
        return None
    week_ago = (today - dt.timedelta(days=LOSS_WINDOW_DAYS)).isoformat()
    heat_before, _ = _latest(history, HEAT_KEY, on_or_before=week_ago)
    alt_before, _ = _latest(history, ALT_KEY, on_or_before=week_ago)
    return {
        "heat_pct": heat, "heat_class": heat_class(heat), "heat_date": heat_date,
        "altitude_m": alt, "altitude_class": altitude_class(alt), "altitude_date": alt_date,
        "lost_heat": (heat is not None and heat_before is not None
                      and heat_before - heat >= HEAT_LOSS_PP),
        "lost_altitude": (alt is not None and alt_before is not None
                          and alt_before - alt >= ALT_LOSS_M),
    }


def guard_value(st: Optional[dict]) -> Optional[str]:
    """What the "mention a class change once" guard stores: both classes in one string."""
    if not st:
        return None
    return f"heat:{st.get('heat_class') or '-'}|alt:{st.get('altitude_class') or '-'}"


def change_context(st: Optional[dict], previous_guard: Optional[str]) -> Optional[dict]:
    """The daily report's ``acclimation`` block — ONLY when the class differs from the one
    last mentioned (``previous_guard``, from bot_state). The very first reading is a
    baseline, not news: no previous guard → None (the caller stores it silently)."""
    if not st or previous_guard is None or guard_value(st) == previous_guard:
        return None
    prev = dict(part.split(":", 1) for part in previous_guard.split("|") if ":" in part)
    lines = []
    if st.get("heat_class") and prev.get("heat") not in (None, "-", st["heat_class"]):
        word = "втрачається" if st.get("lost_heat") else "змінилась"
        lines.append(f"акліматизація до спеки {word}: {CLASS_LABEL[prev['heat']]} → "
                     f"{CLASS_LABEL[st['heat_class']]} ({st['heat_pct']:.0f}%)")
    if st.get("altitude_class") and prev.get("alt") not in (None, "-", st["altitude_class"]):
        word = "втрачається" if st.get("lost_altitude") else "змінилась"
        lines.append(f"висотна акліматизація {word}: {CLASS_LABEL[prev['alt']]} → "
                     f"{CLASS_LABEL[st['altitude_class']]} (~{st['altitude_m']:.0f} м)")
    if not lines:
        return None
    return {"changes": lines, "heat_class": st.get("heat_class"),
            "altitude_class": st.get("altitude_class"),
            "lost_heat": st.get("lost_heat"), "lost_altitude": st.get("lost_altitude")}


def heat_pace_pct(feels_c, klass: Optional[str]) -> Optional[float]:
    """% slower than the cool-weather target for this feels-like temperature and class, from
    the fixed table. ``None`` when there is nothing to adjust (cool day, unknown class)."""
    if not isinstance(feels_c, (int, float)) or klass not in ACCLIMATION_FACTOR:
        return None
    base = 0.0
    for floor, pct in HEAT_PACE_PCT:
        if feels_c >= floor:
            base = pct
    if base <= 0:
        return None
    return round(base * ACCLIMATION_FACTOR[klass], 1)


def race_context(st: Optional[dict], forecast_day: Optional[dict],
                 heat_feels_c: float) -> Optional[dict]:
    """The race pack's ``acclimation`` block — None without acclimation data (the pack is
    then byte-for-byte what it was before NF-31: same context, same cache key)."""
    if not st or not st.get("heat_class"):
        return None
    out = {"heat_class": st["heat_class"], "heat_class_label": CLASS_LABEL[st["heat_class"]],
           "heat_pct": st["heat_pct"]}
    feels = (forecast_day or {}).get("feels_max_c")
    adj = heat_pace_pct(feels, st["heat_class"])
    if adj:
        out["pace_adjust_pct"] = adj
        out["race_feels_c"] = feels
    if st["heat_class"] != "acclimated" and isinstance(feels, (int, float)) \
            and feels >= heat_feels_c:
        out["advice"] = ("Ти ще не адаптований до такої спеки: до старту бігай легкі сесії в "
                         "ту саму пору доби, що й старт, — часткова адаптація встигає за 5–7 днів.")
    return out


def weather_context(st: Optional[dict]) -> Optional[dict]:
    """EP-13's weather-plan check: the class rides along so "too hot" can be read for THIS
    athlete. No new Claude call — the context of the call that already happens."""
    if not st or not (st.get("heat_class") or st.get("altitude_class")):
        return None
    return {k: v for k, v in {
        "heat_class": st.get("heat_class"), "heat_pct": st.get("heat_pct"),
        "altitude_class": st.get("altitude_class"), "altitude_m": st.get("altitude_m"),
        "lost_heat": st.get("lost_heat") or None,
    }.items() if v is not None}
