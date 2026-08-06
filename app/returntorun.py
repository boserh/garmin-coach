"""NF-30 · return-to-run — a deterministic walk/run protocol instead of "carry on as planned".

The system could already WARN about injury risk (NF-04) and could rebuild a plan around
illness or travel (NF-03). Between them sat a hole at exactly the point where a runner is
most vulnerable: **the pain has already happened**. Today a ``subjective.pain`` flag raises
the risk score and may nudge the adaptation to lower volume — and that is all. There is no
plan for coming back, so the runner either stops running for a month or jumps onto a long run
four days later and gets hurt again. A second injury from the same cause is the most expensive
outcome of a season, and preventing it is the whole reason a coach exists.

NF-03 does not fit: illness is a pause and a return to the same plan, while coming back from
pain is a *different progression* — load rebuilt from zero with a pain test at every step, and
its own stopping rule.

Zero LLM by construction (the ladder is deterministic — see the AC), zero network, no state of
its own: the caller stores the returned dict and hands it back next time.

**Boundaries, deliberately.** No diagnosis, no naming an injury, no rehab exercises. The only
medical sentence this module will ever produce is "see a specialist", and it produces it on
the stop rule. That is a product boundary, not a technical limitation — and it is tested.
"""
import datetime as dt
from typing import List, Optional, Sequence

# ---------- trigger ----------

# Pain on this many of the last ``TRIGGER_WINDOW_RUNS`` runs → offer the protocol. Two of five
# rather than one: a single "yes it hurt" after a stumble is not a pattern, and the ticket's
# own risk note is a false trigger from a one-off.
# Where the protocol's state blob lives in ``bot_state`` — shared by the bot (which
# drives the ladder) and the web (UI-05, which shows the current rung).
STATE_KEY = "rtr_state"

TRIGGER_PAIN_RUNS = 2
TRIGGER_WINDOW_RUNS = 5

# ---------- the ladder ----------

# Pain (0-10) at or under this DURING the session and the NEXT MORNING → step up. Above it,
# repeat the same step. This is the entire progression rule, and it is the runner's own
# subjective number (EP-12's scale), not something inferred from the watch.
PAIN_OK = 2

# Pain rising on this many consecutive steps → stop the protocol and point at a professional.
STOP_ON_RISING = 2

# Each rung: a session that can be pushed to the watch as-is (walk/run intervals are ordinary
# structured steps — ``app.garmin.workout_export`` already supports them, no new DTO).
STEPS: List[dict] = [
    {"n": 1, "label": "ходьба 30 хв", "walk_min": 30, "run_min": 0, "reps": 0},
    {"n": 2, "label": "5× (1 хв біг / 4 хв ходьба)", "walk_min": 4, "run_min": 1, "reps": 5},
    {"n": 3, "label": "6× (2 хв біг / 3 хв ходьба)", "walk_min": 3, "run_min": 2, "reps": 6},
    {"n": 4, "label": "5× (4 хв біг / 2 хв ходьба)", "walk_min": 2, "run_min": 4, "reps": 5},
    {"n": 5, "label": "20 хв безперервно легко", "walk_min": 0, "run_min": 20, "reps": 1},
    {"n": 6, "label": "30 хв безперервно легко", "walk_min": 0, "run_min": 30, "reps": 1},
    {"n": 7, "label": "40 хв легко + повернення обсягу", "walk_min": 0, "run_min": 40,
     "reps": 1},
]
LAST_STEP = STEPS[-1]["n"]


def step_by_number(n: int) -> Optional[dict]:
    return next((s for s in STEPS if s["n"] == n), None)


def should_offer(runs: Sequence[dict], *,
                 window: int = TRIGGER_WINDOW_RUNS,
                 needed: int = TRIGGER_PAIN_RUNS) -> Optional[dict]:
    """``{"pain_runs", "window", "note"}`` when the last runs justify offering the protocol,
    else ``None``.

    ``runs`` are EP-12 check-ins (``{date, pain, note}``), oldest-first — the same shape the
    injury radar reads. The decision is only ever an OFFER: the runner presses the button, the
    protocol never starts by itself (the ticket's "людина завжди вирішує сама").
    """
    recent = list(runs)[-window:]
    painful = [r for r in recent if r.get("pain")]
    if len(painful) < needed:
        return None
    notes = [(r.get("note") or "").strip() for r in painful if (r.get("note") or "").strip()]
    return {
        "pain_runs": len(painful),
        "window": len(recent),
        "note": notes[-1] if notes else None,
    }


# ---------- state ----------

def start(today: dt.date) -> dict:
    """A fresh protocol state, parked on the first rung."""
    return {
        "status": "active",
        "step": 1,
        "started": today.isoformat(),
        "last_session": None,
        "repeats": 0,
        "rising": 0,
        "last_pain": None,
    }


def advance(state: dict, pain: Optional[float], *, today: dt.date) -> dict:
    """Move the protocol one session forward. Returns ``(new_state)`` with an ``outcome`` key:

    * ``"idle"`` — no session happened (``pain is None``). The step moves neither up NOR down:
      a day the runner simply didn't run is not evidence of anything (an AC).
    * ``"up"`` — pain within :data:`PAIN_OK` → next rung.
    * ``"repeat"`` — it still hurt → the same rung again.
    * ``"stop"`` — pain rose on :data:`STOP_ON_RISING` consecutive steps → the protocol stops
      itself and stops proposing progression, with a pointer to a professional.
    * ``"done"`` — the last rung cleared.
    """
    s = dict(state or {})
    if s.get("status") != "active":
        return {**s, "outcome": "idle"}
    if pain is None:
        return {**s, "outcome": "idle"}

    prev_pain = s.get("last_pain")
    s["last_pain"] = pain
    s["last_session"] = today.isoformat()

    if pain <= PAIN_OK:
        s["rising"] = 0
        s["repeats"] = 0
        if s.get("step", 1) >= LAST_STEP:
            return {**s, "status": "done", "outcome": "done"}
        s["step"] = int(s.get("step", 1)) + 1
        return {**s, "outcome": "up"}

    # It hurt. Rising pain twice in a row is the stop rule — the protocol will not try to
    # progress through a body that keeps saying no.
    if prev_pain is not None and pain > prev_pain:
        s["rising"] = int(s.get("rising", 0)) + 1
    else:
        s["rising"] = 0
    s["repeats"] = int(s.get("repeats", 0)) + 1
    if s["rising"] >= STOP_ON_RISING:
        return {**s, "status": "stopped", "outcome": "stop"}
    return {**s, "outcome": "repeat"}


def is_active(state: Optional[dict]) -> bool:
    return bool(state) and state.get("status") == "active"


# ---------- the session itself ----------

def session_steps(step: dict) -> List[dict]:
    """Structured workout steps for one rung, in the app's own ``PlanStep`` shape.

    Walk/run is expressed with the interval steps the exporter already understands — the
    ticket is explicit that no new DTO is needed. A walking block rides as a ``recovery``
    step, which is what the watch shows as an untargeted easy segment."""
    if not step:
        return []
    if not step.get("run_min"):
        return [{"kind": "recovery", "dur_s": int(step["walk_min"] * 60),
                 "note": "ходьба, спокійно"}]
    run = {"kind": "run", "dur_s": int(step["run_min"] * 60), "hr_zone": 2}
    if not step.get("walk_min"):
        return [{"kind": "warmup", "dur_s": 300, "note": "ходьба"},
                run,
                {"kind": "cooldown", "dur_s": 300, "note": "ходьба"}]
    return [
        {"kind": "warmup", "dur_s": 300, "note": "ходьба"},
        {"kind": "repeat", "reps": int(step["reps"]), "steps": [
            run,
            {"kind": "recovery", "dur_s": int(step["walk_min"] * 60), "note": "ходьба"},
        ]},
        {"kind": "cooldown", "dur_s": 300, "note": "ходьба"},
    ]


def session_minutes(step: dict) -> int:
    """Approximate wall-clock length of a rung's session, for the plan view."""
    if not step:
        return 0
    if not step.get("run_min"):
        return int(step["walk_min"])
    reps = max(1, int(step.get("reps") or 1))
    return int(10 + reps * (step["run_min"] + step["walk_min"]))


# ---------- text (descriptive tone only — no diagnoses, ever) ----------

def offer_text(trigger: dict) -> str:
    runs = trigger["pain_runs"]
    line = (f"🩹 Ти позначив(ла) біль на {runs} з останніх {trigger['window']} пробіжок.")
    if trigger.get("note"):
        line += f" Останнє: «{trigger['note']}»."
    return (
        line + "\n\nМожу поставити план на паузу й вести тебе покроковим поверненням: "
        "ходьба → біг/ходьба інтервалами → безперервно легко → повернення обсягу. "
        "Крок піднімається лише тоді, коли біль ≤ 2/10 і під час сесії, і наступного ранку.\n"
        "Це не лікування — просто обережна прогресія навантаження."
    )


def step_text(state: dict) -> str:
    """What to do next, given the current state."""
    step = step_by_number(state.get("step", 1))
    if not step:
        return "Протокол завершено."
    head = f"🚶 Крок {step['n']}/{LAST_STEP}: {step['label']}"
    if state.get("repeats"):
        head += f" (повтор {state['repeats']})"
    return (
        f"{head}\n"
        f"Орієнтовно {session_minutes(step)} хв. Після сесії познач біль (0-10) — "
        "і ще раз наступного ранку. Крок вгору лише коли обидва рази ≤ 2."
    )


def outcome_text(state: dict) -> Optional[str]:
    """The message for what just happened, or ``None`` for an uneventful day."""
    outcome = state.get("outcome")
    if outcome == "up":
        return "✅ Біль у межах — піднімаю на наступний крок.\n\n" + step_text(state)
    if outcome == "repeat":
        return ("↩️ Ще відчувається — повторюємо той самий крок, без підвищення.\n\n"
                + step_text(state))
    if outcome == "stop":
        return (
            "🛑 Біль зростає другий крок поспіль — зупиняю протокол і більше не пропоную "
            "підвищення.\n"
            "Це той випадок, коли варто звернутись до фахівця (лікар/фізіотерапевт): "
            "далі має вирішувати людина, яка може тебе оглянути.\n"
            "Коли будеш готовий(а) — можемо зібрати новий план через /plan."
        )
    if outcome == "done":
        return (
            "🎉 Протокол пройдено — навантаження повернулось до безперервного легкого бігу.\n"
            "Можу повернути твій план із паузи (/plan) або зібрати новий з урахуванням "
            "втрачених тижнів."
        )
    return None
