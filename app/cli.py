"""Command-line admin tasks.

Run with the venv interpreter, e.g. create the first (admin) account and seed its
Garmin/Claude/Telegram credentials from the existing ``.env``::

    ./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env

``--seed-env`` requires ``APP_SECRET_KEY`` (the creds are encrypted at rest).
"""
import argparse
import asyncio
import getpass
import pathlib
import sys

from sqlalchemy import text

from app.core.config import settings
from app.core.crypto import encrypt, hash_password
from app.db import users
from app.db.base import async_session_maker, init_db


async def _import_garth_token(email: str) -> int:
    garth_dir = pathlib.Path.home() / ".garth"
    if not garth_dir.exists():
        print("~/.garth not found.")
        return 1
    try:
        import garth
        garth.resume(str(garth_dir))
        token = garth.client.dumps()
    except Exception as e:
        print(f"Failed to read garth token: {e}")
        return 1
    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        user.garth_token_enc = encrypt(token)
        await session.commit()
        print(f"Garth token imported for {email}.")
    return 0


async def _import_fit_series(email: str, path: str, since: str) -> int:
    """Backfill runs' pace/HR series from the export's FIT files (offline, no API)."""
    from app.garmin.export_import import import_fit_series

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        stats = await import_fit_series(session, user.id, path, since=since)
    if stats.get("error"):
        print(stats["error"])
        return 1
    print(f"Added pace/HR series to {stats['series_added']}/{stats['runs']} run(s).")
    return 0


async def _import_export(email: str, path: str, overwrite: bool, since: str) -> int:
    """Backfill daily_metrics from a Garmin GDPR export folder (offline, no API)."""
    from app.garmin.export_import import import_export

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        stats = await import_export(session, user.id, path, overwrite=overwrite, since=since)
    print(f"Inserted {stats['inserted']} new day(s); filled {stats['filled']} existing; "
          f"{stats['unchanged']} unchanged ({stats['parsed']} parsed).")
    return 0


async def _backfill_series(email: str, since: str) -> int:
    """Fetch the pace/HR series for this user's already-stored runs that don't have
    one yet (saved before the feature existed, or imported from the export). Idempotent —
    only fills nulls. ``since`` (ISO) limits to recent runs so it isn't hundreds of API
    calls at once."""
    import asyncio

    from fastapi.concurrency import run_in_threadpool
    from sqlalchemy import select

    from app.db.models import ActivityRecord
    from app.garmin import client
    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        # JSON None is stored as JSON `null` (not SQL NULL), so filter in Python.
        stmt = select(ActivityRecord).where(
            ActivityRecord.user_id == user.id,
            ActivityRecord.type.like("%run%"),
        )
        if since:
            stmt = stmt.where(ActivityRecord.date >= since)
        rows = [r for r in (await session.execute(
            stmt.order_by(ActivityRecord.date.desc()))).scalars().all() if not r.series]
        if not rows:
            print("No runs need backfilling.")
            return 0
        print(f"Backfilling {len(rows)} run(s) for {email}...")
        done = 0
        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            for r in rows:
                sr = await run_in_threadpool(client.fetch_activity_series, r.activity_id)
                if sr:
                    r.series = sr
                    done += 1
                    print(f"  {r.date} {r.type} (id={r.activity_id}) — {len(sr)} pts")
                await asyncio.sleep(0.3)  # be gentle on Garmin
            await session.commit()
        print(f"Done: {done}/{len(rows)} updated.")
    return 0


async def _backfill_auto_activities(email: str, since: str) -> int:
    """Re-fetch dailyEvents from Garmin for stored days that have no auto_activities
    in extra. Idempotent — skips rows that already have the key."""
    import asyncio
    import datetime as dt

    from fastapi.concurrency import run_in_threadpool
    from sqlalchemy import select

    from app.db.models import DailyMetric
    from app.garmin import client
    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime
    from app.garmin.service import _auto_activities

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1

        stmt = select(DailyMetric).where(DailyMetric.user_id == user.id)
        if since:
            stmt = stmt.where(DailyMetric.date >= since)
        stmt = stmt.order_by(DailyMetric.date.desc())
        rows = (await session.execute(stmt)).scalars().all()
        rows = [r for r in rows if not (r.extra or {}).get("auto_activities")]
        if not rows:
            print("Nothing to backfill.")
            return 0

        print(f"Backfilling auto_activities for {len(rows)} day(s)...")
        done = 0
        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            for r in rows:
                date_obj = dt.date.fromisoformat(r.date[:10])
                events = await run_in_threadpool(client.fetch_daily_events, date_obj)
                auto = _auto_activities(events)
                if auto:
                    extra = dict(r.extra or {})
                    extra["auto_activities"] = auto
                    r.extra = extra
                    done += 1
                    print(f"  {r.date}: {auto}")
                await asyncio.sleep(0.3)
            await session.commit()
        print(f"Done: {done}/{len(rows)} day(s) updated.")
    return 0


async def _push_plan(email: str, days: int, dry_run: bool, date: str = None) -> int:
    """Push the user's active-plan workouts in the next ``days`` to the Garmin calendar.

    A rolling window like Runna's — only upcoming ``planned`` running sessions are sent,
    and each is recorded (``garmin_workout_id``/``garmin_schedule_id``) so re-runs skip
    what's already there (idempotent). ``--date`` pushes exactly that one session instead
    of the window (for testing / re-pushing a single edit). ``--dry-run`` builds + prints
    the payloads without writing to Garmin."""
    import datetime as dt

    from fastapi.concurrency import run_in_threadpool

    from app.garmin import plan_sync, repository, workout_export
    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        upcoming = await repository.list_workouts(session, plan.id, upcoming_only=True)
        todo = [w for w in upcoming
                if (w.date == date if date else w.date <= end)
                and plan_sync._pushable(w)
                and w.garmin_workout_id is None]
        if not todo:
            scope = date if date else f"next {days} days"
            print(f"Nothing to push ({scope} already up to date).")
            return 0

        where = date if date else f"through {end}"
        print(f"{'[dry-run] ' if dry_run else ''}Pushing {len(todo)} workout(s) "
              f"for {email} ({where})...")
        if dry_run:
            for w in todo:
                if w.garmin_template_id:
                    print(f"  {w.date}  🏋️ {w.description or 'Силова'}  "
                          f"(clone template {w.garmin_template_id})")
                else:
                    payload = workout_export.build_workout(w)
                    n = len(payload["workoutSegments"][0]["workoutSteps"])
                    print(f"  {w.date}  {payload['workoutName']}  ({n} step(s))")
            return 0

        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            done = 0
            for w in todo:
                wid = await plan_sync.push_workout(session, w)
                if wid:
                    done += 1
                    print(f"  {w.date}  {workout_export.workout_name(w)}  → workout {wid}")
                await asyncio.sleep(0.3)  # be gentle on Garmin
        print(f"Done: {done}/{len(todo)} pushed to the Garmin calendar.")
    return 0


async def _unpush_plan(email: str, date: str = None) -> int:
    """Remove from the Garmin calendar everything we pushed for the active plan, and
    clear the stored ids (so a later push re-creates them fresh). ``--date`` limits it to
    one session. Only touches workouts we created (by saved ``garmin_workout_id``) — never
    your manual/Runna workouts."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import plan_sync, repository
    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        pushed = [w for w in await repository.list_workouts(session, plan.id)
                  if w.garmin_workout_id is not None
                  and (w.date == date if date else True)]
        if not pushed:
            print("Nothing pushed for this plan.")
            return 0
        print(f"Removing {len(pushed)} pushed workout(s) for {email}...")
        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            for w in pushed:
                wid = w.garmin_workout_id
                if await plan_sync.remove_workout(session, w):
                    print(f"  {w.date}  removed workout {wid}")
                else:
                    print(f"  {w.date}  workout {wid} already gone")
                await asyncio.sleep(0.3)
        print("Done.")
    return 0


async def _list_workouts(email: str) -> int:
    """Print the user's saved Garmin workouts (id · sport · name) — to find the strength
    routines (Day 1 / Day 2) to reference in the plan."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import client
    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            rows = await run_in_threadpool(client.fetch_workouts)
    if not rows:
        print("No saved workouts found.")
        return 0
    for w in rows:
        print(f"  {w['id']}  [{w['sport'] or '—'}]  {w['name']}")
    return 0


async def _token_expiry() -> int:
    """OPS-01: read-only decode of every user's stored garth token — when does each
    user's OAuth1 token (≈1 year from issue) die, i.e. the plan-B migration deadline.
    Raw SQL on purpose: a diagnostic tool must work even on a half-migrated DB."""
    from app.core.crypto import decrypt
    from app.garmin.token_info import decode_token_info

    def fmt(ts):
        return ts.strftime("%Y-%m-%d") if ts else "—"

    async with async_session_maker() as session:
        rows = await session.execute(
            text("SELECT id, email, garth_token_enc FROM users ORDER BY id")
        )
        for uid, email, token_enc in rows:
            if not token_enc:
                print(f"  {uid}  {email}: no stored garth token")
                continue
            try:
                info = decode_token_info(decrypt(token_enc))
            except Exception as e:
                print(f"  {uid}  {email}: undecodable token ({e})")
                continue
            print(
                f"  {uid}  {email}: oauth1 issued {fmt(info['oauth1_issued'])}"
                f" → dies ≈ {fmt(info['oauth1_expiry_est'])}"
                f"  (oauth2 exp {fmt(info['oauth2_expires_at'])},"
                f" refresh exp {fmt(info['oauth2_refresh_expires_at'])})"
            )
    return 0


async def _create_user(email: str, password: str, is_admin: bool, seed_env: bool) -> int:
    await init_db()  # zero-config safety; Alembic remains the source of truth
    async with async_session_maker() as session:
        if await users.get_by_email(session, email):
            print(f"User {email} already exists.")
            return 1
        user = await users.create_user(
            session, email=email, password_hash=hash_password(password), is_admin=is_admin
        )
        if seed_env:
            if settings.GARMIN_EMAIL:
                user.garmin_email_enc = encrypt(settings.GARMIN_EMAIL)
            if settings.GARMIN_PASSWORD:
                user.garmin_password_enc = encrypt(settings.GARMIN_PASSWORD)
            if settings.ANTHROPIC_API_KEY:
                user.anthropic_key_enc = encrypt(settings.ANTHROPIC_API_KEY)
            if settings.TELEGRAM_CHAT_ID:
                user.telegram_chat_id = settings.TELEGRAM_CHAT_ID
            # Import garth token from ~/.garth if it exists.
            garth_dir = pathlib.Path.home() / ".garth"
            if garth_dir.exists():
                try:
                    import garth
                    garth.resume(str(garth_dir))
                    user.garth_token_enc = encrypt(garth.client.dumps())
                    print("Imported garth token from ~/.garth.")
                except Exception as e:
                    print(f"Warning: could not import garth token: {e}")
            # Claim pre-existing single-user data (rows the migration left unowned).
            claimed = 0
            for tbl in ("daily_metrics", "activities", "report_logs"):
                res = await session.execute(
                    text(f"UPDATE {tbl} SET user_id = :uid WHERE user_id IS NULL"),
                    {"uid": user.id},
                )
                claimed += res.rowcount or 0
            await session.commit()
            print("Seeded Garmin/Claude/Telegram credentials from .env (encrypted).")
            print(f"Claimed {claimed} pre-existing data rows for this user.")
        print(f"Created user {email} (id={user.id}, admin={is_admin}).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cu = sub.add_parser("create-user", help="Create a web-login user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--password", help="login password (prompted securely if omitted)")
    cu.add_argument("--admin", action="store_true", help="grant admin (can add users)")
    cu.add_argument(
        "--seed-env", action="store_true",
        help="encrypt Garmin/Claude/Telegram creds from .env into this user",
    )

    igt = sub.add_parser("import-garth-token", help="Import ~/.garth token into a user's DB record")
    igt.add_argument("--email", required=True)

    bf = sub.add_parser("backfill-series", help="Fetch pace/HR series for stored runs missing one")
    bf.add_argument("--email", required=True)
    bf.add_argument("--since", help="only runs from this ISO date onward (e.g. 2025-06-01)")

    baa = sub.add_parser(
        "backfill-auto-activities",
        help="Re-fetch auto-detected activities for stored days missing them",
    )
    baa.add_argument("--email", required=True)
    baa.add_argument("--since", help="only days from this ISO date onward (e.g. 2025-06-01)")

    ie = sub.add_parser("import-export", help="Backfill daily_metrics from a Garmin GDPR export")
    ie.add_argument("--email", required=True)
    ie.add_argument("--path", required=True, help="export folder (top-level or DI_CONNECT)")
    ie.add_argument("--since", help="only import from this ISO date onward (e.g. 2025-06-01)")
    ie.add_argument("--overwrite", action="store_true", help="overwrite days already stored")

    fs = sub.add_parser("import-fit-series", help="Runs' pace/HR series from export FIT files")
    fs.add_argument("--email", required=True)
    fs.add_argument("--path", required=True, help="export folder (needs DI-Connect-Uploaded-Files)")
    fs.add_argument("--since", help="only runs from this ISO date onward")

    pp = sub.add_parser("push-plan", help="Push upcoming plan workouts to the Garmin calendar")
    pp.add_argument("--email", required=True)
    pp.add_argument("--days", type=int, default=14, help="rolling window size (default 14)")
    pp.add_argument("--date", help="push only the session on this ISO date (overrides --days)")
    pp.add_argument("--dry-run", action="store_true", help="build + print payloads, don't write")

    up = sub.add_parser("unpush-plan", help="Remove pushed plan workouts from the Garmin calendar")
    up.add_argument("--email", required=True)
    up.add_argument("--date", help="remove only the session on this ISO date")

    lw = sub.add_parser("list-workouts", help="List the user's saved Garmin workouts (id/name)")
    lw.add_argument("--email", required=True)

    sub.add_parser(
        "token-expiry",
        help="Decode all users' stored garth tokens: OAuth1 issue/expiry dates (read-only)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if not password:
            parser.error("password must not be empty")
        return asyncio.run(_create_user(args.email, password, args.admin, args.seed_env))
    if args.cmd == "import-garth-token":
        return asyncio.run(_import_garth_token(args.email))
    if args.cmd == "backfill-series":
        return asyncio.run(_backfill_series(args.email, args.since))
    if args.cmd == "backfill-auto-activities":
        return asyncio.run(_backfill_auto_activities(args.email, args.since))
    if args.cmd == "import-export":
        return asyncio.run(_import_export(args.email, args.path, args.overwrite, args.since))
    if args.cmd == "import-fit-series":
        return asyncio.run(_import_fit_series(args.email, args.path, args.since))
    if args.cmd == "push-plan":
        return asyncio.run(_push_plan(args.email, args.days, args.dry_run, args.date))
    if args.cmd == "unpush-plan":
        return asyncio.run(_unpush_plan(args.email, args.date))
    if args.cmd == "list-workouts":
        return asyncio.run(_list_workouts(args.email))
    if args.cmd == "token-expiry":
        return asyncio.run(_token_expiry())
    return 0


if __name__ == "__main__":
    sys.exit(main())
