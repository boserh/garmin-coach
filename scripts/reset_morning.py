"""Drop cached Claude answers so the next morning report is generated fresh.

Why this exists: the daily/morning report is dedup-cached in the ``llm_cache`` table
(PERF-02) for a week, keyed on a hash of the payload + date + question + model + all the
extra context. That's what makes a second ``/report`` press free — but it also means that
after a **prompt or context change** (e.g. the day-label fix), re-running the same morning
tick over unchanged data returns the OLD text from cache instead of exercising the new
code. Purge the entry and the next run really calls Claude.

Pure DB work: this script never calls Anthropic or Garmin itself. It only makes the NEXT
report a real (paid) call instead of a cache hit — that's the whole point, so run it
deliberately, not on a schedule.

Usage (venv interpreter, from the repo root)::

    ./venv/bin/python -m scripts.reset_morning --dry-run      # show what would go
    ./venv/bin/python -m scripts.reset_morning                # today's cached answers
    ./venv/bin/python -m scripts.reset_morning --days 3       # last 3 days of entries
    ./venv/bin/python -m scripts.reset_morning --all          # the whole llm_cache

    # also let the scheduled morning DM fire again today (see the warning below):
    ./venv/bin/python -m scripts.reset_morning --email me@example.com --resend

Two separate things, deliberately split:

1. **The cache** (``llm_cache``). Purging it is cheap and safe — a cache miss only means
   the next identical question is paid for again. NB the rows carry no ``user_id`` and no
   ``kind``: the key is an opaque sha256, so "only the morning report" cannot be selected.
   ``--days`` is the honest approximation (entries created in the last N days) — on a
   single-user Pi that's a handful of rows.
2. **The once-a-day guard** (``bot_state`` key ``morning_sent_date``, per user). Clearing
   it with ``--resend`` makes ``morning_job`` send the morning DM **again today** on its
   next tick — i.e. a real, paid Opus/Sonnet call plus a Telegram message, without you
   pressing anything. It only fires inside the 07:00-12:00 window in that user's timezone;
   outside it nothing happens. If you just want to LOOK at a regenerated report, prefer the
   admin bot's ``/test_morning`` — it runs the exact same path without consuming (or
   needing) the guard. Then this script's cache purge alone is enough.

Nothing here touches ``report_logs`` — that's the cost audit trail (a real call always
leaves a row), and rewriting history would defeat it.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from sqlalchemy import delete, func, select

from app.db.base import async_session_maker, init_db
from app.db.models import BotState, LlmCache

MORNING_STATE_KEY = "morning_sent_date"   # bot.jobs.MORNING_STATE_KEY


def _cutoff(days: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


async def purge_cache(session, *, days: int | None, dry_run: bool) -> int:
    """Delete cached Claude answers — everything when ``days`` is None, otherwise the
    entries created within the last ``days`` days. Returns how many rows matched."""
    where = () if days is None else (LlmCache.created_at >= _cutoff(days),)
    n = await session.scalar(select(func.count()).select_from(LlmCache).where(*where))
    if n and not dry_run:
        await session.execute(delete(LlmCache).where(*where))
        await session.commit()
    return n or 0


class UserNotFound(Exception):
    """``--email`` resolved to nothing — reported as an error, not as "guard was empty"."""


async def clear_morning_guard(session, email: str, *, dry_run: bool) -> str | None:
    """Clear one user's ``morning_sent_date`` so today's morning DM can fire again.
    Returns the guard value that was there, or None when it wasn't set."""
    from app.db import users

    user = await users.get_by_email(session, email)
    if user is None:
        raise UserNotFound(email)
    row = await session.get(BotState, (user.id, MORNING_STATE_KEY))
    if row is None or not row.value:
        return None
    was = row.value
    if not dry_run:
        await session.delete(row)
        await session.commit()
    return was


async def _run(args: argparse.Namespace) -> int:
    await init_db()
    async with async_session_maker() as session:
        days = None if args.all else args.days
        n = await purge_cache(session, days=days, dry_run=args.dry_run)
        scope = "усі" if days is None else f"за останні {days} дн"
        verb = "знайдено (dry-run)" if args.dry_run else "видалено"
        print(f"llm_cache: {verb} {n} запис(ів) — {scope}.")

        if args.resend:
            if not args.email:
                print("--resend потребує --email <користувач>.", file=sys.stderr)
                return 2
            try:
                was = await clear_morning_guard(session, args.email, dry_run=args.dry_run)
            except UserNotFound:
                print(f"Користувача {args.email} не знайдено.", file=sys.stderr)
                return 1
            if was is None:
                print(f"morning_sent_date: гард для {args.email} і так порожній.")
            else:
                verb = "буде знято" if args.dry_run else "знято"
                print(f"morning_sent_date: {verb} (було {was}) — ранковий звіт "
                      f"надішлеться ще раз у вікні 07-12 (реальний виклик Claude).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Почистити дедуп-кеш Claude, щоб ранковий звіт згенерувався наново.")
    p.add_argument("--days", type=int, default=1,
                   help="видалити записи кешу, створені за останні N днів (типово 1)")
    p.add_argument("--all", action="store_true",
                   help="видалити ВЕСЬ llm_cache, а не тільки свіжі записи")
    p.add_argument("--email", help="користувач для --resend")
    p.add_argument("--resend", action="store_true",
                   help="також зняти гард morning_sent_date, щоб ранковий звіт "
                        "надіслався сьогодні ще раз (це платний виклик Claude)")
    p.add_argument("--dry-run", action="store_true",
                   help="лише показати, що буде видалено")
    args = p.parse_args(argv)
    if args.days < 0:
        p.error("--days має бути >= 0")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
