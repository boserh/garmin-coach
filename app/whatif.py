"""NF-29 · «А що, якщо…» — a scenario simulator with **zero** Claude calls.

``loadforecast`` already knows what a planned session weighs and what ACWR the current week
lands on; ``goal`` already projects a fitness trend to a race date. Nobody could ask them a
*hypothetical* question — "and if I add a fourth run?", "and if I'm away for a week?", "and if
the race moves three weeks later?" — except through ``/ask``, i.e. by paying for an LLM call
that guesses at arithmetic we can do exactly and for free.

This module is that arithmetic, and nothing else:

* :class:`Inputs` — what the athlete's next weeks look like today (the active plan's still-
  ``planned`` sessions, or, without a plan, the typical week from their own history), plus
  the completed weeks' actual load and the goal-trend inputs. Assembled from the DB by
  ``repository.whatif_inputs``; everything here is pure and in-memory.
* :class:`Scenario` — four knobs: sessions per week ±, volume ±%, race date ± weeks, an
  absence of N days starting today.
* :func:`simulate` — the baseline and the scenario through the SAME week model, side by side:
  per-week load, run km, a rolling chronic load and its ACWR. The colour of an ACWR comes from
  ``loadforecast.acwr_level`` with the live ``FORECAST_ACWR_*`` thresholds — the ones this
  week's forecast is shown with (NF-04's ``ACWR_HIGH``/``ACWR_VERY_HIGH`` are the same
  140/160 % in percent units) — never a second copy of the numbers.

The chronic load **rolls forward with the simulated weeks**, which is what makes the "skip a
week" scenario honest: the absence drags the 4-week average down, so the first normal week
back reads as a spike — the classic return-from-holiday injury pattern.

Deliberately not here: physiology (VO2max from volume — false precision we have no basis
for) and any free-text fallback. A request the bot can't parse gets the syntax hint, never an
LLM call (:func:`parse`), or this stops being the free path.
"""
import datetime as dt
import re
from dataclasses import dataclass, field, replace
from statistics import median
from typing import List, Optional

from app import goal as goal_mod
from app import loadforecast, plankind

HORIZON_WEEKS = 4             # this week + the next three — where a plan change actually lands
MAX_SESSIONS_DELTA = 3
MAX_VOLUME_PCT = 50
MAX_SHIFT_WEEKS = 12
MAX_SKIP_DAYS = 28
DEFAULT_EXTRA_KM = 6.0        # an added session when nothing else tells us its length
EXTRA_SESSION_TYPE = "easy"   # an extra session is an easy one — the safe default to model
_EASY_TYPES = {"easy", "recovery"}

SESSIONS_OPTIONS = list(range(-MAX_SESSIONS_DELTA, MAX_SESSIONS_DELTA + 1))
VOLUME_OPTIONS = list(range(-MAX_VOLUME_PCT, MAX_VOLUME_PCT + 1, 10))
SHIFT_OPTIONS = [-8, -6, -4, -3, -2, -1, 0, 1, 2, 3, 4, 6, 8, 12]
SKIP_OPTIONS = [0, 3, 5, 7, 10, 14, 21, 28]


@dataclass(frozen=True)
class Scenario:
    sessions_delta: int = 0       # ± sessions per week
    volume_pct: int = 0           # ± % on every session's length (and so its load)
    target_shift_weeks: int = 0   # race date ± weeks
    skip_days: int = 0            # away for N days starting today

    def clamped(self) -> "Scenario":
        def c(v, lo, hi):
            return max(lo, min(hi, int(v or 0)))
        return Scenario(
            sessions_delta=c(self.sessions_delta, -MAX_SESSIONS_DELTA, MAX_SESSIONS_DELTA),
            volume_pct=c(self.volume_pct, -MAX_VOLUME_PCT, MAX_VOLUME_PCT),
            target_shift_weeks=c(self.target_shift_weeks, -MAX_SHIFT_WEEKS, MAX_SHIFT_WEEKS),
            skip_days=c(self.skip_days, 0, MAX_SKIP_DAYS),
        )

    @property
    def is_baseline(self) -> bool:
        return self == Scenario()


@dataclass
class Inputs:
    today: dt.date
    done_load: float = 0.0                 # this ISO week's actual load so far
    done_km: float = 0.0                   # …and run km
    past_weekly_loads: List[float] = field(default_factory=list)  # completed weeks, oldest 1st
    history_days: int = 0
    anchor_pace: Optional[float] = None
    planned: List[dict] = field(default_factory=list)   # {date, type, dist_km, steps}
    has_plan: bool = False
    # No plan → the athlete's typical week: {sessions, session_load, session_km}.
    typical: Optional[dict] = None
    # Goal-trend inputs (active plan only): {history, metric_key, label, higher_better,
    # target_date, target_s}.
    goal: Optional[dict] = None


# ---------- the week model ----------

def _week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _horizon(inputs: Inputs, scenario: Scenario) -> int:
    """Enough weeks to see the return from an absence, never fewer than the default."""
    back = (inputs.today.weekday() + scenario.skip_days) // 7 + 2
    return max(HORIZON_WEEKS, min(back, HORIZON_WEEKS + 3))


def _is_run(type_str: Optional[str]) -> bool:
    return (type_str or "").lower() in plankind.RUN_TYPES


def _session(date: dt.date, type_: str, dist_km: Optional[float], load: float,
             steps=None) -> dict:
    return {"date": date, "type": type_, "dist_km": dist_km, "load": load, "steps": steps}


def _baseline_sessions(inputs: Inputs, weeks: int) -> List[dict]:
    """The sessions the athlete is heading into, dated, each with its estimated load."""
    start = inputs.today
    end = _week_start(start) + dt.timedelta(weeks=weeks) - dt.timedelta(days=1)
    out = []
    if inputs.has_plan:
        for s in inputs.planned:
            try:
                d = dt.date.fromisoformat(s.get("date"))
            except (TypeError, ValueError):
                continue
            if start <= d <= end:
                out.append(_session(d, s.get("type"), s.get("dist_km"),
                                    loadforecast.session_load(s, inputs.anchor_pace),
                                    s.get("steps")))
        return out
    t = inputs.typical or {}
    n = int(t.get("sessions") or 0)
    if n <= 0:
        return out
    for w in range(weeks):
        monday = _week_start(start) + dt.timedelta(weeks=w)
        for i in range(n):
            d = monday + dt.timedelta(days=min(6, round(i * 7 / n)))
            if start <= d <= end:
                out.append(_session(d, "run", t.get("session_km"),
                                    float(t.get("session_load") or 0.0)))
    return out


def _extra_km(inputs: Inputs, sessions: List[dict]) -> float:
    if not inputs.has_plan:
        return float((inputs.typical or {}).get("session_km") or DEFAULT_EXTRA_KM)
    easy = [s["dist_km"] for s in sessions
            if (s["type"] or "").lower() in _EASY_TYPES and s.get("dist_km")]
    return round(float(median(easy)), 1) if easy else DEFAULT_EXTRA_KM


def _apply(inputs: Inputs, scenario: Scenario, sessions: List[dict], weeks: int) -> List[dict]:
    """The scenario's sessions — volume first, then ± sessions, then the absence (so an added
    session that falls inside it is gone too, as it would be in life)."""
    out = [dict(s) for s in sessions]
    if scenario.volume_pct:
        k = 1 + scenario.volume_pct / 100.0
        for s in out:
            s["load"] = round(s["load"] * k, 1)
            if s.get("dist_km"):
                s["dist_km"] = round(s["dist_km"] * k, 1)

    if scenario.sessions_delta:
        extra_km = _extra_km(inputs, sessions)
        if inputs.has_plan:
            extra_load = loadforecast.session_load(
                {"type": EXTRA_SESSION_TYPE, "dist_km": extra_km}, inputs.anchor_pace)
        else:
            extra_load = float((inputs.typical or {}).get("session_load") or 0.0)
        extra_km *= 1 + scenario.volume_pct / 100.0
        extra_load *= 1 + scenario.volume_pct / 100.0
        for w in range(weeks):
            monday = _week_start(inputs.today) + dt.timedelta(weeks=w)
            days = [monday + dt.timedelta(days=i) for i in range(7)]
            days = [d for d in days if d >= inputs.today]
            in_week = [s for s in out if monday <= s["date"] < monday + dt.timedelta(days=7)]
            if scenario.sessions_delta > 0:
                taken = {s["date"] for s in in_week}
                free = [d for d in days if d not in taken] or days
                for i in range(scenario.sessions_delta):
                    if not free:
                        break
                    out.append(_session(free[i % len(free)], EXTRA_SESSION_TYPE,
                                        round(extra_km, 1), round(extra_load, 1)))
            else:
                # Drop the lightest sessions first: the long run and the quality day are the
                # ones a real athlete keeps when the week gets short.
                for s in sorted(in_week, key=lambda s: s["load"])[:-scenario.sessions_delta]:
                    out.remove(s)

    if scenario.skip_days:
        until = inputs.today + dt.timedelta(days=scenario.skip_days)
        out = [s for s in out if not (inputs.today <= s["date"] < until)]
    return sorted(out, key=lambda s: s["date"])


def _trace(inputs: Inputs, sessions: List[dict], weeks: int, *, warn: float,
           high: float) -> List[dict]:
    """Week by week: load, run km, the rolling chronic load (the last
    ``loadforecast.MIN_CHRONIC_WEEKS`` weeks, simulated ones included) and its ACWR."""
    n_chronic = loadforecast.MIN_CHRONIC_WEEKS
    history = list(inputs.past_weekly_loads[-n_chronic:])
    calibrating = inputs.history_days < loadforecast.MIN_HISTORY_DAYS
    rows = []
    for w in range(weeks):
        monday = _week_start(inputs.today) + dt.timedelta(weeks=w)
        sunday = monday + dt.timedelta(days=6)
        in_week = [s for s in sessions if monday <= s["date"] <= sunday]
        load = sum(s["load"] for s in in_week) + (inputs.done_load if w == 0 else 0.0)
        km = sum((s.get("dist_km") or 0.0) for s in in_week if _is_run(s["type"])
                 or not inputs.has_plan) + (inputs.done_km if w == 0 else 0.0)
        window = history[-n_chronic:]
        chronic = sum(window) / len(window) if window else 0.0
        acwr = round(load / chronic, 2) if (chronic > 0 and not calibrating) else None
        rows.append({
            "week": monday.strftime("%G-W%V"), "start": monday, "end": sunday,
            "load": round(load, 1), "km": round(km, 1), "sessions": len(in_week),
            "chronic": round(chronic, 1),
            "acwr": acwr,
            "level": loadforecast.acwr_level(acwr, warn_acwr=warn, high_acwr=high)
            if acwr is not None else None,
        })
        history.append(load)
    return rows


# ---------- the goal half ----------

def _shift_date(date_s: Optional[str], weeks: int) -> Optional[str]:
    try:
        return (dt.date.fromisoformat(date_s) + dt.timedelta(weeks=weeks)).isoformat()
    except (TypeError, ValueError):
        return None


def _goal(inputs: Inputs, scenario: Scenario) -> Optional[dict]:
    """Baseline vs shifted-date projection of the goal metric. ``None`` without an active
    plan, a goal trend, or (for the shift) a target date — those sections then stay silent."""
    g = inputs.goal
    if not inputs.has_plan or not g:
        return None
    kw = dict(metric_key=g["metric_key"], higher_better=g["higher_better"],
              target_s=g.get("target_s"), today=inputs.today)
    base = goal_mod.project(g["history"], target_date=g.get("target_date"), **kw)
    if base is None:
        return None
    new_date = g.get("target_date")
    if scenario.target_shift_weeks and g.get("target_date"):
        new_date = _shift_date(g["target_date"], scenario.target_shift_weeks)
    scen = goal_mod.project(g["history"], target_date=new_date, **kw)
    is_time = g["metric_key"].startswith("race_")
    return {"label": g["label"], "is_time": is_time, "base": base, "scen": scen,
            "base_txt": _fmt_goal_value(base.get("projected"), is_time),
            "scen_txt": _fmt_goal_value((scen or {}).get("projected"), is_time),
            "target_date": g.get("target_date"), "new_target_date": new_date}


# ---------- the whole simulation ----------

def _flags(base: List[dict], scen: List[dict]) -> List[str]:
    """What the scenario changes about risk, in plain words — only what got WORSE (or
    better) against the baseline, never a restatement of the baseline itself."""
    out = []
    rank = {None: 0, "ok": 0, "warn": 1, "high": 2}
    for b, s in zip(base, scen):
        if rank[s["level"]] > rank[b["level"]]:
            word = "червона зона" if s["level"] == "high" else "жовта зона"
            was = f" (було {b['acwr']:.2f})" if b["acwr"] is not None else ""
            out.append(f"Тиждень з {s['start']:%d.%m}: ACWR {s['acwr']:.2f} — {word}{was}.")
        elif rank[s["level"]] < rank[b["level"]] and b["acwr"] is not None:
            out.append(f"Тиждень з {s['start']:%d.%m}: ACWR {b['acwr']:.2f} → "
                       f"{s['acwr']:.2f}, нижче за поріг ризику.")
    if base and scen and base[-1]["chronic"] > 0:
        drop = (scen[-1]["chronic"] - base[-1]["chronic"]) / base[-1]["chronic"] * 100
        if drop <= -10:
            out.append(f"Хронічне навантаження нижче на {abs(drop):.0f}% — форма просідає, "
                       "а повернення до звичного обсягу читатиметься як стрибок.")
    return out


def _apply_instruction(inputs: Inputs, scenario: Scenario, goal: Optional[dict]) -> Optional[str]:
    """The plan-edit request this scenario stands for — handed to the EXISTING paid,
    confirm-first path (/chat, /plan), never applied from here. Only for the two scenarios a
    plan edit can actually express: a moved race and an absence."""
    if not inputs.has_plan:
        return None
    parts = []
    if scenario.target_shift_weeks and goal and goal.get("new_target_date"):
        parts.append(f"Старт переноситься з {goal['target_date']} на "
                     f"{goal['new_target_date']} — перебудуй план під нову дату.")
    if scenario.skip_days:
        last = inputs.today + dt.timedelta(days=scenario.skip_days - 1)
        parts.append(f"Мене не буде з {inputs.today.isoformat()} по {last.isoformat()} — "
                     "прибери тренування на ці дні й обережно сплануй повернення.")
    return " ".join(parts) or None


def simulate(inputs: Inputs, scenario: Scenario, *, warn_acwr: float = loadforecast.ACWR_WARN,
             high_acwr: float = loadforecast.ACWR_HIGH) -> dict:
    """Baseline vs scenario. Pure and in-memory: nothing here can write, and the inputs are
    not mutated (the scenario works on copies)."""
    scenario = scenario.clamped()
    weeks = _horizon(inputs, scenario)
    base_sessions = _baseline_sessions(inputs, weeks)
    scen_sessions = _apply(inputs, scenario, base_sessions, weeks)
    base = _trace(inputs, base_sessions, weeks, warn=warn_acwr, high=high_acwr)
    scen = _trace(inputs, scen_sessions, weeks, warn=warn_acwr, high=high_acwr)
    goal = _goal(inputs, scenario)
    return {
        "scenario": scenario,
        "weeks": [{"base": b, "scen": s} for b, s in zip(base, scen)],
        "flags": _flags(base, scen),
        "calibrating": inputs.history_days < loadforecast.MIN_HISTORY_DAYS,
        "has_plan": inputs.has_plan,
        "has_data": bool(base_sessions) or any(inputs.past_weekly_loads),
        "goal": goal,
        "apply": _apply_instruction(inputs, scenario, goal),
    }


# ---------- the bot's text form ----------

HELP = (
    "🔮 /whatif — сценарій без витрат (рахує, а не вгадує):\n"
    "• /whatif +1 сесія — ще одне тренування на тиждень (або -1)\n"
    "• /whatif обсяг +20% — довші сесії (або -20%)\n"
    "• /whatif старт +3 тижні — старт пізніше (або -2)\n"
    "• /whatif пропуск 7д — мене не буде N днів з сьогодні\n"
    "Можна поєднувати: /whatif +1 сесія обсяг -10%"
)

_RE_SHIFT = re.compile(r"(?:старт|забіг|race)\s*([+-]?\d+)\s*(?:тиж\w*|т|w)?")
_RE_SKIP_WEEKS = re.compile(r"(?:пропуск\w*|пропущу|skip|відпустк\w*)\s*(\d+)\s*(?:тиж\w*|w)")
_RE_SKIP = re.compile(r"(?:пропуск\w*|пропущу|skip|відпустк\w*)\s*(\d+)\s*(?:дн\w*|ден\w*|д|d)?")
_RE_VOLUME = re.compile(r"(?:обсяг|об'єм|обʼєм|км|volume)?\s*([+-]\d+)\s*%")
_RE_SESSIONS = re.compile(r"([+-]\d+)\s*(?:сесі\w*|трен\w*|пробіж\w*|раз\w*|session\w*)?")


def parse(text: Optional[str]) -> Optional[Scenario]:
    """A bounded, deterministic phrase parser. Anything it doesn't fully understand → None,
    and the caller shows :data:`HELP` — never an LLM fallback (the ticket's main pitfall)."""
    s = (text or "").lower().replace(",", " ").strip()
    if not s:
        return None
    found = {}

    def take(regex, key, scale=1):
        nonlocal s
        m = regex.search(s)
        if not m:
            return
        found[key] = int(m.group(1)) * scale
        s = (s[:m.start()] + " " + s[m.end():]).strip()

    take(_RE_SHIFT, "target_shift_weeks")
    take(_RE_SKIP_WEEKS, "skip_days", 7)
    if "skip_days" not in found:
        take(_RE_SKIP, "skip_days")
    take(_RE_VOLUME, "volume_pct")
    take(_RE_SESSIONS, "sessions_delta")
    if not found or s.strip(" .;:-"):
        return None
    sc = Scenario(**found)
    return None if sc.is_baseline else sc


def _fmt_goal_value(v: Optional[float], is_time: bool) -> str:
    if v is None:
        return "—"
    return goal_mod.fmt_time(v) if is_time else f"{v:g}"


def describe(scenario: Scenario) -> str:
    bits = []
    if scenario.sessions_delta:
        bits.append(f"{scenario.sessions_delta:+d} сесія/тиж")
    if scenario.volume_pct:
        bits.append(f"обсяг {scenario.volume_pct:+d}%")
    if scenario.target_shift_weeks:
        bits.append(f"старт {scenario.target_shift_weeks:+d} тиж")
    if scenario.skip_days:
        bits.append(f"пропуск {scenario.skip_days} дн")
    return ", ".join(bits) or "без змін"


def summary(result: dict) -> str:
    """Deterministic Ukrainian text for ``/whatif`` — no LLM."""
    lines = [f"🔮 Сценарій: {describe(result['scenario'])}", ""]
    if not result["has_data"]:
        return "\n".join(lines + ["Поки немає ні плану, ні історії тренувань — нема що рахувати."])
    lines.append("Тиждень · навантаження зараз → сценарій · ACWR")
    for row in result["weeks"]:
        b, s = row["base"], row["scen"]
        acwr = ""
        if b["acwr"] is not None:
            mark = {"high": " 🔴", "warn": " 🟡"}.get(s["level"], "")
            acwr = f" · {b['acwr']:.2f} → {s['acwr']:.2f}{mark}"
        lines.append(f"з {b['start']:%d.%m}: {b['load']:.0f} → {s['load']:.0f} "
                     f"({b['km']:.0f} → {s['km']:.0f} км){acwr}")
    if result["calibrating"]:
        lines.append("ACWR ще калібрується — замало історії для чесного відношення.")
    if result["flags"]:
        lines.append("")
        lines += [f"⚠️ {f}" for f in result["flags"]]
    g = result.get("goal")
    if g and result["scenario"].target_shift_weeks:
        lines.append("")
        lines.append(f"🎯 {g['label'].capitalize()}: на {g['target_date']} — {g['base_txt']}, "
                     f"на {g['new_target_date']} — {g['scen_txt']}")
        if g["scen"] and g["scen"].get("projected") is None:
            lines.append("Нова дата задалеко для чесної проєкції — лише тренд.")
        lines.append("Це тренд прогнозу Garmin, не гарантований результат.")
    elif result["scenario"].target_shift_weeks and not result["has_plan"]:
        lines.append("")
        lines.append("Без активного плану немає дати старту — сценарій цілі пропущено.")
    lines.append("")
    lines.append("Навантаження — оцінка (тривалість × інтенсивність), не точний TRIMP.")
    return "\n".join(lines)


def scenario_from_query(params) -> Scenario:
    """Build a scenario from form/query values, tolerating junk (→ 0)."""
    def i(key):
        try:
            return int(params.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return replace(Scenario(), sessions_delta=i("sessions"), volume_pct=i("volume"),
                   target_shift_weeks=i("shift"), skip_days=i("skip")).clamped()
