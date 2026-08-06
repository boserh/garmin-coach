"""What a fresh account still has to do before the coach works — as data.

A registration used to end on the login page with one sentence ("чекай підтвердження"),
and the first login dropped the user into ``/settings`` — a form of eleven fields with no
statement of which three actually matter or what happens once they're filled. Nothing
anywhere said the Telegram bot had to be connected at all; people found the app silent
and assumed it was broken.

So the steps live here, in one list, computed from flags: the page renders them, the nav
counts them, the dashboard banner links to them, and the bot's own replies can say which
one is still missing. Pure — no ORM, no network — so the router maps a ``User`` onto the
flags and this module decides what's done, what's next, and what to say about it.

Following ``app.banners``: a step is a dict, the caller renders it, and the "show it or
not" decision stays with the data.
"""
from __future__ import annotations

from typing import Optional

# The three credentials the app cannot run without, plus the payoff step. Order is the
# order they're done in: Garmin first (it's the data), then the key that analyses it,
# then the bot that delivers it.
REQUIRED_KEYS = ("garmin", "claude", "telegram")


def _step(key: str, title: str, done: bool, *, why: str, how: list[str],
          action: str = "", action_text: str = "", note: str = "",
          note_level: str = "", required: bool = True) -> dict:
    """One setup step. ``note``/``note_level`` carry a state that isn't just done/not —
    a saved Garmin password that Garmin has since rejected is "filled in" and still
    broken, and the page has to say so rather than show a tick."""
    return {"key": key, "title": title, "done": done, "why": why, "how": how,
            "action": action, "action_text": action_text, "note": note,
            "note_level": note_level, "required": required}


def build_steps(
    *,
    has_garmin: bool,
    garmin_connected: bool = False,
    garmin_invalid: bool = False,
    has_anthropic: bool,
    has_telegram: bool,
    has_plan: bool = False,
    telegram_link: Optional[str] = None,
) -> list[dict]:
    """The full checklist for one account, newest state applied.

    ``telegram_link`` is the one-click ``t.me/...?start=`` deep link (see
    ``app.core.tglink``); None means it isn't available in this deployment and the step
    falls back to telling the user to paste their chat id by hand.
    """
    garmin_note, garmin_level = "", ""
    if garmin_invalid:
        garmin_note = "Garmin відхиляє збережений пароль — онови його, синк стоїть."
        garmin_level = "danger"
    elif has_garmin and not garmin_connected:
        garmin_note = "Креденшели збережено, сесії ще немає — натисни «Перевірити з'єднання»."
        garmin_level = "warn"
    elif garmin_connected:
        garmin_note = "Сесію Garmin створено."
        garmin_level = "ok"

    telegram_how = (
        ["Тисни кнопку — Telegram відкриється на боті, і акаунт підключиться сам."]
        if telegram_link else
        ["Напиши @userinfobot у Telegram — він поверне твій numeric chat ID.",
         "Встав цей ID у полі «Telegram chat ID» у Налаштуваннях."]
    )

    # A saved-but-rejected Garmin password is not a done step, whatever is in the DB.
    garmin_done = has_garmin and not garmin_invalid

    return [
        _step(
            "garmin", "Підключити Garmin Connect", garmin_done,
            why="Звідки беруться сон, HRV, стрес і тренування. Без цього аналізувати нічого.",
            how=["Введи email і пароль від Garmin Connect — зберігаються зашифровано.",
                 "Натисни «Перевірити з'єднання»; якщо Garmin попросить код (MFA), "
                 "поле для нього з'явиться там же."],
            action="/settings#garmin",
            action_text="Оновити креденшели →" if garmin_done else "Ввести креденшели →",
            note=garmin_note, note_level=garmin_level,
        ),
        _step(
            "claude", "Додати ключ Claude API", has_anthropic,
            why="Ключ, яким оплачується аналіз. Він твій — витрати йдуть на твій рахунок, "
                "їх видно в /costs і на сторінці витрат.",
            how=["Створи ключ на console.anthropic.com → API keys.",
                 "Поповни баланс у Billing — без нього ключ повертає помилку.",
                 "Встав ключ у Налаштуваннях (показується назад він ніколи)."],
            action="/settings#claude",
            action_text="Замінити ключ →" if has_anthropic else "Вставити ключ →",
        ),
        _step(
            "telegram", "Підключити Telegram-бота", has_telegram,
            why="Куди приходять ранковий звіт, питання про самопочуття після пробіжки й "
                "пропозиції змінити план. Веб працює й без бота, але автоматика — через нього.",
            how=telegram_how,
            # Done → the settings field (change the linked chat); not done → the one-tap
            # deep link when we can offer it, the manual chat-id field when we can't.
            action=("/settings#telegram" if has_telegram
                    else (telegram_link or "/settings#telegram")),
            action_text=("Змінити чат →" if has_telegram
                         else ("Підключити Telegram →" if telegram_link else "Ввести chat ID →")),
        ),
        _step(
            "plan", "Створити програму тренувань", has_plan, required=False,
            why="Саме звідси беруться заплановані тренування, порівняння план/факт "
                "і синк на годинник.",
            how=["Відкрий «Програму» і заповни форму: мета, днів на тиждень, довгий забіг.",
                 "Генерація враховує твої останні тренування — краще після першого синку Garmin."],
            action="/plan",
            action_text="Відкрити програму →" if has_plan else "Скласти план →",
        ),
    ]


def progress(steps: list[dict]) -> tuple[int, int]:
    """(done, total) over the REQUIRED steps only — the optional plan step must not
    make a fully-configured account look unfinished."""
    required = [s for s in steps if s["required"]]
    return sum(1 for s in required if s["done"]), len(required)


def is_complete(steps: list[dict]) -> bool:
    done, total = progress(steps)
    return done == total


def next_step(steps: list[dict]) -> Optional[dict]:
    """The first thing left to do, required steps before the optional one."""
    for s in steps:
        if s["required"] and not s["done"]:
            return s
    for s in steps:
        if not s["done"]:
            return s
    return None


def missing_labels(steps: list[dict]) -> list[str]:
    """Short names of the unfinished required steps — for a one-line summary in a bot
    reply or a banner, where the full checklist doesn't fit.

    Reads required steps only, so a caller that just wants this line can call
    :func:`build_steps` without ``has_plan`` (and without the query behind it).
    """
    names = {"garmin": "Garmin", "claude": "ключ Claude", "telegram": "Telegram"}
    return [names.get(s["key"], s["key"])
            for s in steps if s["required"] and not s["done"]]
