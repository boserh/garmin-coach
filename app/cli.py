"""Command-line admin tasks.

Run with the venv interpreter, e.g. create the first (admin) account and seed its
Garmin/Claude/Telegram credentials from the existing ``.env``::

    ./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env

``--seed-env`` requires ``APP_SECRET_KEY`` (the creds are encrypted at rest).
"""
import argparse
import asyncio
import getpass
import sys

from app.core.config import settings
from app.core.crypto import encrypt, hash_password
from app.db import users
from app.db.base import async_session_maker, init_db


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
            await session.commit()
            print("Seeded Garmin/Claude/Telegram credentials from .env (encrypted).")
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

    args = parser.parse_args(argv)
    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if not password:
            parser.error("password must not be empty")
        return asyncio.run(_create_user(args.email, password, args.admin, args.seed_env))
    return 0


if __name__ == "__main__":
    sys.exit(main())
