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
    print(f"Imported {stats['imported']} day(s); skipped {stats['skipped_existing']} "
          f"already-present; {stats['parsed']} parsed.")
    return 0


async def _backfill_series(email: str) -> int:
    """Fetch the pace/HR series for this user's already-stored runs that don't have
    one yet (saved before the feature existed). Idempotent — only fills nulls."""
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
        rows = (await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.series.is_(None),
                ActivityRecord.type.like("%run%"),
            ).order_by(ActivityRecord.date.desc())
        )).scalars().all()
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

    ie = sub.add_parser("import-export", help="Backfill daily_metrics from a Garmin GDPR export")
    ie.add_argument("--email", required=True)
    ie.add_argument("--path", required=True, help="export folder (top-level or DI_CONNECT)")
    ie.add_argument("--since", help="only import from this ISO date onward (e.g. 2025-06-01)")
    ie.add_argument("--overwrite", action="store_true", help="overwrite days already stored")

    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if not password:
            parser.error("password must not be empty")
        return asyncio.run(_create_user(args.email, password, args.admin, args.seed_env))
    if args.cmd == "import-garth-token":
        return asyncio.run(_import_garth_token(args.email))
    if args.cmd == "backfill-series":
        return asyncio.run(_backfill_series(args.email))
    if args.cmd == "import-export":
        return asyncio.run(_import_export(args.email, args.path, args.overwrite, args.since))
    return 0


if __name__ == "__main__":
    sys.exit(main())
