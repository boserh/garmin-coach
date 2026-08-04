"""Next-checkup reminders — the "Аналізи" tab's second follow-up (the first being
on-demand Claude interpretation, ``app.analysis.reports.run_checkup_analysis``).

Pure Python, zero LLM, zero DB — mirrors ``app.gear``'s split: the pure decision logic
lives here, the DB read + Telegram DM live in ``bot.jobs._checkup_reminder_for_user``
(guarded per-checkup, once-only, via ``bot_state``)."""
import datetime as dt
from typing import List

# Nudge once a checkup's `next_due_date` is within this many days — or already past
# (a negative day-count below is still "due", just overdue).
REMINDER_LEAD_DAYS = 7

# bot_state key prefix, + the checkup's own DB id AND its current `next_due_date`: a
# reminder is sent at most once per (checkup, due date) pair — editing `next_due_date`
# on the same row changes the guard key, so a deliberate reschedule re-arms the
# reminder instead of staying silent forever.
REMINDER_PREFIX = "checkup_reminder:"


def guard_key(row) -> str:
    """The per-(checkup, due date) bot_state guard key — bumping `next_due_date` on an
    existing row naturally re-arms the reminder since it changes this key."""
    return f"{REMINDER_PREFIX}{row.id}:{row.next_due_date}"


def due(rows: list, today: dt.date, lead_days: int = REMINDER_LEAD_DAYS) -> List:
    """Rows (``HealthCheckup``, must carry ``next_due_date``) whose due date falls within
    ``lead_days`` of ``today`` or has already passed. A malformed date is skipped rather
    than raising — a bad manual entry should never break the whole reminder pass."""
    out = []
    for r in rows:
        if not r.next_due_date:
            continue
        try:
            d = dt.date.fromisoformat(r.next_due_date)
        except ValueError:
            continue
        if (d - today).days <= lead_days:
            out.append(r)
    return out


def reminder_text(row, today: dt.date) -> str:
    """One DM's worth of text for a due/overdue checkup reminder."""
    try:
        d = dt.date.fromisoformat(row.next_due_date)
        days = (d - today).days
    except ValueError:
        days = None
    if days is None:
        when = row.next_due_date
    elif days < 0:
        when = f"прострочено на {-days} дн. ({row.next_due_date})"
    elif days == 0:
        when = f"сьогодні ({row.next_due_date})"
    else:
        when = f"через {days} дн. ({row.next_due_date})"
    return (
        f"🔬 Час запланувати наступний чекап: «{row.title}» — {when}.\n"
        f"Запис зроблено за результатами від {row.date}. Деталі — в «Аналізи» в застосунку."
    )
