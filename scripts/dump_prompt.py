"""Print the EXACT request the morning report sends to Claude — without sending it.

The request body is not stored anywhere. ``report_logs`` keeps the question string, the
token counts and the answer, but never the ``user_content`` JSON that was actually posted
— so "what did the analyst see this morning?" cannot be answered from the DB after the
fact. This script answers it the only honest way: it runs the **real** code path
(``build_payload_cached`` → ``run_analysis``, with every context builder — previous_report,
plan_today, fitness, norm, subjective, health_alerts, fueling, intensity, athlete_profile,
away, weather) and intercepts the call one line before ``messages.create``, dumping the
request instead of posting it.

Two interceptions, both process-local, neither writes anything:

* ``reports._get_client`` → a stub whose ``messages.create`` raises a private exception.
  ``analyze_with_stats`` only catches ``APIStatusError``/``APIConnectionError`` and
  ``_run_cached_narration`` only catches ``AnalystError``, so it propagates out **before**
  ``llm_cache.put`` and **before** ``log_report`` — no cache row, no ReportLog row, no
  cost, no Telegram message.
* ``llm_cache.get`` → always a miss, so the prompt is really assembled instead of the run
  short-circuiting on a cached answer. Nothing is written back (see above).

READ THIS BEFORE TRUSTING THE OUTPUT: this is the request as it would be built **now**,
not a recording of an earlier one. ``recent_activities`` is a live Garmin fetch on every
payload build, so a request from a past morning cannot be reproduced — if that fetch
failed then, the only surviving evidence is in scripts/diag_morning.py. To make future
requests recoverable byte-for-byte, set ``PROMPT_DUMP_DIR`` (see app/analysis/dump.py):
every real call then writes its own request to disk as it goes out.

Costs: **0 Anthropic calls** (that is the whole point). It DOES do a normal Garmin fetch
and persists what it fetches, exactly like any /report — no more, no less.

Usage (venv interpreter, from the repo root)::

    ./venv/bin/python -m scripts.dump_prompt --email me@example.com
    ./venv/bin/python -m scripts.dump_prompt --email me@example.com --system
    ./venv/bin/python -m scripts.dump_prompt --email me@example.com --out /tmp/req.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

# The morning report's question, verbatim from bot.jobs._MORNING_Q — the daily /report
# uses reports._DEFAULT_DAILY_Q instead, which run_analysis fills in for an empty string.
MORNING_Q = "Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."


class _Captured(Exception):
    """Carries the kwargs ``messages.create`` was called with. Deliberately NOT an
    AnalystError: every ``except`` between here and the caller is narrower than this, so
    it escapes without triggering the error-logging path."""

    def __init__(self, kwargs: dict) -> None:
        super().__init__("request captured")
        self.kwargs = kwargs


class _StubMessages:
    @staticmethod
    def create(**kwargs: Any):
        raise _Captured(kwargs)


class _StubClient:
    messages = _StubMessages()


def _install_stubs() -> None:
    from app.analysis import reports
    from app.db import llm_cache

    reports._get_client = lambda api_key=None: _StubClient()

    async def _always_miss(*_a, **_kw):
        return None

    llm_cache.get = _always_miss


def _sizes(user_content: dict) -> list[tuple[str, int, str]]:
    """Per-key byte size of the request, biggest first — an empty ``recent_activities``
    is visible here at a glance, without reading the whole JSON."""
    out = []
    for key, value in user_content.items():
        blob = json.dumps(value, ensure_ascii=False)
        note = ""
        if key == "data" and isinstance(value, dict):
            acts = value.get("recent_activities")
            note = (f"recent_activities: {len(acts)} шт" if isinstance(acts, list)
                    else "recent_activities: ВІДСУТНЄ")
        out.append((key, len(blob.encode("utf-8")), note))
    return sorted(out, key=lambda r: -r[1])


async def _run(args: argparse.Namespace) -> int:
    _install_stubs()

    from app.analysis.reports import run_analysis
    from app.core.tz import user_today
    from app.db import users as users_db
    from app.db.base import async_session_maker, init_db
    from app.garmin import service
    from app.garmin.runtime import user_runtime
    from app import weather as weather_mod

    await init_db()
    async with async_session_maker() as session:
        user = await users_db.get_by_email(session, args.email)
        if user is None:
            print(f"Користувача {args.email} не знайдено.", file=sys.stderr)
            return 1

        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                print("Немає Garmin-кредів — payload не зібрати.", file=sys.stderr)
                return 1
            # Exactly bot.jobs._tick_for_user / force_morning_for_user.
            payload, _ = await service.build_payload_cached(
                session, user.id, days=args.days, activity_limit=args.activity_limit)
            wx = await weather_mod.forecast_for_user(session, user)

            captured: Optional[_Captured] = None
            try:
                # Exactly delivery.build_report(kind="morning").
                await run_analysis(
                    session, payload, user_id=user.id,
                    question=MORNING_Q if args.kind == "morning" else "",
                    kind=args.kind, api_key=creds.anthropic_key, weather=wx,
                    today=user_today(user),
                )
            except _Captured as e:
                captured = e
            else:
                print("Запит не перехоплено — виклик пройшов повз стаб. "
                      "Перевір, чи не змінився шлях reports._get_client.", file=sys.stderr)
                return 3

    kwargs = captured.kwargs
    body = json.loads(kwargs["messages"][0]["content"])

    print(f"model       : {kwargs.get('model')}")
    print(f"max_tokens  : {kwargs.get('max_tokens')}")
    print(f"thinking    : {kwargs.get('thinking')}")
    system = kwargs.get("system") or ""
    print(f"system      : {len(system)} символів "
          f"({'нижче' if args.system else 'сховано, --system щоб показати'})")

    print("\nРОЗМІР ЗАПИТУ ПО КЛЮЧАХ (байт):")
    for key, size, note in _sizes(body):
        print(f"  {key:<18} {size:>8}   {note}")

    if args.system:
        print(f"\n{'=' * 78}\nSYSTEM\n{'=' * 78}\n{system}")

    print(f"\n{'=' * 78}\nUSER CONTENT (те, що йде в messages[0].content)\n{'=' * 78}")
    pretty = json.dumps(body, ensure_ascii=False, indent=2)
    print(pretty)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"model": kwargs.get("model"),
                       "max_tokens": kwargs.get("max_tokens"),
                       "thinking": kwargs.get("thinking"),
                       "system": system,
                       "user_content": body}, fh, ensure_ascii=False, indent=2)
        print(f"\nЗаписано у {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Надрукувати точний запит ранкового звіту до Claude, не надсилаючи його.")
    p.add_argument("--email", required=True)
    p.add_argument("--kind", default="morning", choices=("morning", "report"),
                   help="morning = питання ранкового звіту; report = дефолтне денне")
    p.add_argument("--days", type=int, default=3,
                   help="вікно daily[], як у ранковому тіку (типово 3)")
    p.add_argument("--activity-limit", type=int, default=20,
                   help="скільки активностей тягнути, як у тіку (типово 20)")
    p.add_argument("--system", action="store_true", help="показати і системний промпт")
    p.add_argument("--out", help="записати весь запит у JSON-файл")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
