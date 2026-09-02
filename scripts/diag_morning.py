"""Post-mortem for ONE morning report: what the analyst actually had in hand.

Why this exists: when the morning report describes a day wrongly, there are two very
different causes and the delivered text alone cannot tell them apart —

1. **the context was incomplete** (``recent_activities`` came back empty because the
   Garmin activity-list fetch failed: ``client._safe`` returns ``{"_error": ...}`` and
   ``service._activity_rows`` turns any non-list into ``[]``, silently — nothing
   downstream distinguishes "trained nothing" from "could not read"), or
2. **the context was complete and the model narrated it badly** (e.g. carrying a
   relative word like "позавчора" over from ``previous_report``).

This script pulls the four surviving traces of that one request and puts them side by
side, so the answer is read off the data instead of guessed:

* ``report_logs`` — the audit row of the call: when, which model, and **how big the
  prompt was**. An empty activity list is worth a few thousand input tokens, so today's
  ``input_tokens`` against the recent morning median is the single sharpest signal.
* ``bot_state['garmin_errors']`` — OPS-05's 48h ring buffer of failed Garmin endpoints
  with timestamps. A ``/activitylist-service/activities`` entry inside the report window
  is the smoking gun for cause 1. NB the retention: run this within two days.
* ``daily_metrics`` — the rows the report window covered, including
  ``extra.auto_activities`` (the watch's unconfirmed auto-detections, which the analyst
  reads as that day's real cardio load) and ``updated_at`` (when the row was last
  written, i.e. whether it was served from the DB cache).
* ``activities`` — what the DB holds for those days **now**. A session sitting here that
  the report never mentioned means it either arrived after the report, or the fetch that
  should have carried it failed.
* ``bot.log`` (+ rotations) — the ``MORNING`` / ``GARMIN OK|ERR`` / ``CLAUDE`` lines
  around the report time, which say it outright when the log is still on disk.

Read-only and free: pure DB + log-file reads, **0 Garmin requests and 0 Anthropic calls**
(nothing here imports the analysis service). Safe to run on the Pi at any time.

Usage (venv interpreter, from the repo root)::

    ./venv/bin/python -m scripts.diag_morning --email me@example.com
    ./venv/bin/python -m scripts.diag_morning --email me@example.com --date 2026-09-02
    ./venv/bin/python -m scripts.diag_morning --email me@example.com --text --window 60
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import glob
import json
import os
import re
import statistics
import sys
from typing import Iterable, Optional

from sqlalchemy import select

from app.core.config import settings
from app.core.tz import user_tz
from app.db import users as users_db
from app.db.base import async_session_maker, init_db
from app.db.models import ActivityRecord, BotState, DailyMetric, ReportLog

# bot.jobs sends the morning report through delivery.build_report(kind="morning").
MORNING_KIND = "morning"
GARMIN_ERRORS_KEY = "garmin_errors"          # app.garmin.service.GARMIN_ERRORS_KEY
ACTIVITY_ENDPOINT = "/activitylist-service"  # _endpoint_suffix keeps 2 path segments
# How much lighter than the recent norm a prompt has to be before it is called suspicious.
LIGHT_PROMPT_RATIO = 0.75
# app.core.logging: "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", local time.
LOG_TS_LEN = 19
LOG_TS_FMT = "%Y-%m-%d %H:%M:%S"
LOG_INTEREST = re.compile(
    r"MORNING|PAYLOAD|CLAUDE|TICK skip|GARMIN ERR|activitylist|MATCH |ANALYST")


def _utc(value: dt.datetime) -> dt.datetime:
    """SQLite drops the tzinfo on a ``DateTime(timezone=True)`` column, so a naive value
    read back is UTC by construction (``models._utcnow``) — say so explicitly."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _fmt(value: Optional[dt.datetime], tz) -> str:
    return _utc(value).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _head(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------- 1. the call itself ----------

async def _report_rows(session, user_id: int, since: dt.datetime) -> list[ReportLog]:
    rows = await session.execute(
        select(ReportLog)
        .where(ReportLog.user_id == user_id, ReportLog.created_at >= since)
        .order_by(ReportLog.created_at)
    )
    return list(rows.scalars())


def _show_calls(rows: list[ReportLog], day: dt.date, tz, *, show_text: bool
                ) -> Optional[ReportLog]:
    """Print every Claude call in the window and return the day's morning row (the last
    one, if a re-run produced several)."""
    _head("1. ВИКЛИКИ CLAUDE (report_logs)")
    if not rows:
        print("Жодного рядка — за цей період Claude не викликався взагалі.")
        return None

    print(f"{'час (лок.)':<20} {'kind':<9} {'model':<22} "
          f"{'in':>7} {'out':>6} {'$':>7}  прапорці")
    target: Optional[ReportLog] = None
    for r in rows:
        local = _utc(r.created_at).astimezone(tz)
        flags = []
        if not r.ok:
            flags.append(f"ERROR: {(r.error or '')[:60]}")
        if r.cached:
            flags.append("CACHE HIT (реального виклику не було)")
        mark = ""
        if r.kind == MORNING_KIND and local.date() == day:
            target = r
            mark = "  ◀ цей звіт"
        print(f"{local.strftime('%Y-%m-%d %H:%M:%S'):<20} {r.kind:<9} {r.model[:22]:<22} "
              f"{r.input_tokens:>7} {r.output_tokens:>6} {r.cost_usd:>7.4f}  "
              f"{'; '.join(flags)}{mark}")

    if target is None:
        print(f"\n⚠ За {day} немає рядка kind={MORNING_KIND} — ранковий звіт того дня "
              f"не генерувався (гард morning_sent_date? поза вікном 07-12? "
              f"або віддано з дедуп-кешу — тоді дивись рядок з CACHE HIT).")
        return None

    # The sharp signal: an empty recent_activities strips a few thousand input tokens.
    others = [r.input_tokens for r in rows
              if r.kind == MORNING_KIND and r is not target
              and not r.cached and r.input_tokens]
    print(f"\nПромпт цього звіту: {target.input_tokens} вхідних токенів.")
    if others:
        med = statistics.median(others)
        ratio = target.input_tokens / med if med else 1.0
        print(f"Медіана інших ранкових звітів у вікні ({len(others)} шт): {med:.0f} "
              f"→ {ratio * 100:.0f}% від норми.")
        if ratio < LIGHT_PROMPT_RATIO:
            print("⚠ ПРОМПТ ПОМІТНО ЛЕГШИЙ ЗА НОРМУ — контекст був неповний. "
                  "Найімовірніше recent_activities приїхав порожнім "
                  "(див. розділ 2: провал /activitylist-service).")
        else:
            print("✓ Розмір промпту звичайний — список активностей, найпевніше, БУВ на місці, "
                  "і проблема в наративі моделі, а не в даних.")
    else:
        print("Немає з чим порівняти — збільш --compare-days.")

    if show_text:
        _head("1b. ТЕКСТ, ЩО БУВ НАДІСЛАНИЙ")
        print(target.report_text or "(порожньо)")
    return target


# ---------- 2. Garmin endpoint failures (OPS-05) ----------

async def _show_garmin_errors(session, user_id: int, day: dt.date, tz,
                              report_at: Optional[dt.datetime], window_min: int) -> None:
    _head("2. ЗБОЇ GARMIN (bot_state.garmin_errors, зберігається 48 год)")
    row = await session.get(BotState, (user_id, GARMIN_ERRORS_KEY))
    if row is None or not row.value:
        print("Порожньо: жодного збою не записано (або блоб уже протух — ретеншн 48 год).")
        return
    try:
        recent = (json.loads(row.value) or {}).get("recent") or []
    except (ValueError, TypeError):
        print(f"Блоб не парситься: {row.value[:200]}")
        return

    lo = hi = None
    if report_at is not None:
        lo = _utc(report_at) - dt.timedelta(minutes=window_min)
        hi = _utc(report_at) + dt.timedelta(minutes=window_min)

    hits = 0
    for e in recent:
        ts = dt.datetime.fromtimestamp(e.get("ts") or 0, dt.timezone.utc)
        local = ts.astimezone(tz)
        if local.date() != day:
            continue
        in_window = lo is not None and lo <= ts <= hi
        is_activity_list = str(e.get("endpoint", "")).startswith(ACTIVITY_ENDPOINT)
        mark = ""
        if in_window:
            mark += "  ◀ У ВІКНІ ЗВІТУ"
        if is_activity_list:
            mark += "  ◀◀ СПИСОК АКТИВНОСТЕЙ"
            hits += 1 if in_window else 0
        print(f"{local.strftime('%H:%M:%S')}  {str(e.get('kind')):<8} "
              f"{str(e.get('endpoint')):<38} {str(e.get('detail'))[:70]}{mark}")

    if hits:
        print(f"\n⚠ ЗНАЙДЕНО: {hits} провал(ів) {ACTIVITY_ENDPOINT} у вікні ±{window_min} хв "
              f"навколо звіту. Саме тут recent_activities стає [] "
              f"(service._activity_rows: `if not isinstance(acts, list): return []`).")
    else:
        print(f"\nПровалів {ACTIVITY_ENDPOINT} у вікні ±{window_min} хв немає. "
              f"Якщо активності все одно бракувало — вона, найпевніше, ще не була "
              f"вивантажена в Garmin Connect на момент звіту.")


# ---------- 3-4. what the report window actually contained ----------

async def _show_daily(session, user_id: int, days: Iterable[str], tz) -> None:
    _head("3. ЩОДЕННІ ЗРІЗИ (daily_metrics — вікно звіту, days=3)")
    rows = await session.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user_id, DailyMetric.date.in_(list(days)))
        .order_by(DailyMetric.date)
    )
    for d in rows.scalars():
        extra = d.extra or {}
        print(f"\n{d.date}  hrv={d.hrv_avg} ({d.hrv_status})  sleep={d.sleep_score}  "
              f"bb +{d.bb_charged}/-{d.bb_drained}  stress≤{d.stress_max}  "
              f"readiness={extra.get('readiness_score')}  acwr={extra.get('acwr_pct')}")
        print(f"   рядок оновлено: {_fmt(d.updated_at, tz)} "
              f"(створено {_fmt(d.created_at, tz)})")
        auto = extra.get("auto_activities")
        print(f"   auto_activities: {auto if auto else '—'}"
              + ("   ← аналітик читає це як реальне кардіо того дня" if auto else ""))


async def _show_activities(session, user_id: int, day: dt.date, back: int, tz) -> None:
    _head(f"4. АКТИВНОСТІ В БАЗІ ЗАРАЗ (останні {back} дн)")
    since = (day - dt.timedelta(days=back)).isoformat()
    rows = await session.execute(
        select(ActivityRecord)
        .where(ActivityRecord.user_id == user_id, ActivityRecord.date >= since)
        .order_by(ActivityRecord.date, ActivityRecord.id)
    )
    found = False
    for a in rows.scalars():
        found = True
        print(f"{a.date}  id={a.id:<5} garmin={a.activity_id:<12} {str(a.type):<20} "
              f"{a.dur_min or 0:>6.1f}хв {a.dist_km or 0:>6.2f}км  hr={a.avg_hr}"
              + ("  [ПРИХОВАНА]" if a.is_hidden else ""))
    if not found:
        print("Нічого.")
    print("\nПримітка: в activities немає created_at, тому «коли рядок з'явився» видно лише "
          "непрямо — за зростанням id і за логом (розділ 5). Але recent_activities у звіті "
          "НІКОЛИ не читається з цієї таблиці: це завжди живий запит у Garmin на момент "
          "збірки payload. Тобто сесія, що є тут, але якої немає у звіті, або приїхала "
          "пізніше, або її з'їв провалений фетч.")


# ---------- 5. the log ----------

def _log_files() -> list[str]:
    base = settings.LOG_FILE
    return [p for p in [base] + sorted(glob.glob(f"{base}.*")) if os.path.exists(p)]


def _show_log(day: dt.date, report_at: Optional[dt.datetime], window_min: int) -> None:
    """``asctime`` is written in the PROCESS wall clock (the systemd unit's TZ), not in the
    user's timezone — so the window is built process-local too. On a single-user Pi the two
    are the same zone; for a user elsewhere this keeps the log filter honest anyway."""
    _head(f"5. BOT.LOG (±{window_min} хв навколо звіту, час процесу)")
    files = _log_files()
    if not files:
        print(f"Файл {settings.LOG_FILE} не знайдено — запусти скрипт на Pi з кореня репо.")
        return

    lo = hi = None
    if report_at is not None:
        local = _utc(report_at).astimezone()      # system-local, matching the log
        lo = (local - dt.timedelta(minutes=window_min)).replace(tzinfo=None)
        hi = (local + dt.timedelta(minutes=window_min)).replace(tzinfo=None)
        day = local.date()

    shown = 0
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ts = dt.datetime.strptime(line[:LOG_TS_LEN], LOG_TS_FMT)
                except ValueError:
                    continue          # continuation line of a traceback
                if ts.date() != day:
                    continue
                # The log's asctime is process-local wall clock, same clock as `lo`/`hi`.
                if lo is not None and not (lo <= ts <= hi):
                    continue
                if not LOG_INTEREST.search(line):
                    continue
                print(line.rstrip())
                shown += 1
    if not shown:
        print("Нічого не збіглося — лог міг уже проротуватись (5×1MB), "
              "або звуз/розшир --window.")


# ---------- glue ----------

async def _run(args: argparse.Namespace) -> int:
    await init_db()
    async with async_session_maker() as session:
        user = await users_db.get_by_email(session, args.email)
        if user is None:
            print(f"Користувача {args.email} не знайдено.", file=sys.stderr)
            return 1
        tz = user_tz(user)
        day = (dt.date.fromisoformat(args.date) if args.date
               else dt.datetime.now(tz).date())

        print(f"Користувач: {user.email} (id={user.id}), tz={tz}")
        print(f"Розбір ранкового звіту за: {day}")
        print("Скрипт лише читає БД і лог: 0 запитів у Garmin, 0 викликів Anthropic.")

        since = dt.datetime.combine(
            day - dt.timedelta(days=args.compare_days), dt.time.min, tzinfo=tz
        ).astimezone(dt.timezone.utc)
        rows = await _report_rows(session, user.id, since)
        target = _show_calls(rows, day, tz, show_text=args.text)
        report_at = target.created_at if target else None

        await _show_garmin_errors(session, user.id, day, tz, report_at, args.window)
        window = [(day - dt.timedelta(days=i)).isoformat() for i in (2, 1, 0)]
        await _show_daily(session, user.id, window, tz)
        await _show_activities(session, user.id, day, args.activity_days, tz)
        _show_log(day, report_at, args.window)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Розбір одного ранкового звіту: що аналітик реально мав у руках.")
    p.add_argument("--email", required=True, help="користувач, чий звіт розбираємо")
    p.add_argument("--date", help="дата звіту (YYYY-MM-DD); типово — сьогодні в tz юзера")
    p.add_argument("--compare-days", type=int, default=7,
                   help="скільки днів назад брати інші звіти для порівняння розміру "
                        "промпту (типово 7)")
    p.add_argument("--activity-days", type=int, default=7,
                   help="скільки днів активностей показати (типово 7)")
    p.add_argument("--window", type=int, default=30,
                   help="вікно ± хвилин навколо звіту для збоїв Garmin і логу (типово 30)")
    p.add_argument("--text", action="store_true",
                   help="також надрукувати текст надісланого звіту")
    args = p.parse_args(argv)
    for name in ("compare_days", "activity_days", "window"):
        if getattr(args, name) < 0:
            p.error(f"--{name.replace('_', '-')} має бути >= 0")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
