"""Small shared Ukrainian date/pace formatting primitives (A5).

The weekday-abbreviation list, the abbreviated-month list and the min/km→"M:SS"
pace arithmetic were each copied across ``routers/plan.py``, ``routers/me.py``,
``bot/jobs.py`` and ``records.py``. This centralises just those primitives; the
call sites keep their own thin wrappers where behaviour genuinely differs (the
``/км`` suffix, the error fallback, the input shape), so nothing about the
rendered output changes — only the duplicated constants and arithmetic move here.
"""
import datetime as dt
from typing import Union

WEEKDAYS_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS_ABBR_UK = ["січ", "лют", "бер", "кві", "тра", "чер",
                  "лип", "сер", "вер", "жов", "лис", "гру"]


def dow_abbr(iso: str) -> str:
    """ISO date → Ukrainian weekday abbreviation ('Пн'); '' on a bad/empty date."""
    try:
        return WEEKDAYS_UK[dt.date.fromisoformat(iso).weekday()]
    except (ValueError, TypeError):
        return ""


def day_month(d: Union[dt.date, str]) -> str:
    """``date`` | ISO string → 'day mon' (e.g. '7 лип'). Raises on a bad string —
    callers that need a fallback wrap it (the behaviour differs between them)."""
    if not isinstance(d, dt.date):
        d = dt.date.fromisoformat(d)
    return f"{d.day} {MONTHS_ABBR_UK[d.month - 1]}"


def pace(min_km: float, suffix: str = "") -> str:
    """min/km → 'M:SS' (+ optional suffix like '/км')."""
    total = round(min_km * 60)
    return f"{total // 60}:{total % 60:02d}{suffix}"


def plural_uk(n, one: str, few: str, many: str) -> str:
    """Ukrainian plural form for ``n``: 1 → *one*, 2–4 → *few*, else *many* (with the
    11–14 exception every Slavic pluraliser needs)."""
    n = int(n or 0)
    if n % 100 in (11, 12, 13, 14):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


def sets_word(n) -> str:
    """1 підхід / 2–4 підходи / 5+ підходів — the 'N підходів' header on the
    Garmin-style strength cards."""
    return plural_uk(n, "підхід", "підходи", "підходів")


def sessions_word(n) -> str:
    """1 сесія / 2–4 сесії / 5+ сесій — the count on a collapsed past week (ST-22)."""
    return plural_uk(n, "сесія", "сесії", "сесій")
