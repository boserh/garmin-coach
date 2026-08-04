"""One-off: backfill the last 30 days of Garmin data for an existing user by id.

Same mechanism as `app.cli create-user --backfill-month`, but targets a user that
already exists instead of one just created. Requires the user to already have
Garmin credentials stored (encrypted) in the DB.

Usage:
    ./venv/bin/python -m scripts.backfill_month_by_id --user-id 3
"""
import argparse
import asyncio
import logging
import time

from app.core.logging import setup as setup_logging
from app.db.base import async_session_maker, init_db
from app.db.models import User
from app.garmin.credentials import load_credentials
from app.garmin.providers import GarminAuthFailed
from app.garmin.runtime import user_runtime
from app.garmin.service import build_payload_cached

logger = logging.getLogger("backfill_month_by_id")


async def _run(user_id: int) -> int:
    setup_logging()
    t0 = time.monotonic()
    await init_db()
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            logger.error(f"No user with id {user_id}.")
            return 1

        creds = load_credentials(user)
        if not creds.has_garmin:
            logger.error("No Garmin credentials on this user — set them up in /settings first.")
            return 1

        logger.info(f"Fetching last 30 days of Garmin data (sleep/HRV/stress/body "
                    f"battery/readiness/VO2max + activities) for user {user_id} "
                    f"({user.email})... this can take a while.")
        try:
            async with user_runtime(session, user):
                payload, new_activities = await build_payload_cached(
                    session, user.id, days=30, activity_limit=60,
                )
        except GarminAuthFailed:
            logger.error("Garmin rejected the stored email/password — backfill "
                         "skipped. Fix the creds via /settings and re-run.")
            return 1

        await session.commit()
        elapsed = time.monotonic() - t0
        logger.info(f"Backfilled {len(payload.daily)} day(s), "
                    f"{len(new_activities)} activit{'y' if len(new_activities) == 1 else 'ies'} "
                    f"in {elapsed:.1f}s.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.user_id)))


if __name__ == "__main__":
    main()
