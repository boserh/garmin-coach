"""The demo account: a singleton, read-only walkthrough with seeded fake data.

``ensure_demo_user`` is idempotent — first call creates ``User(is_demo=True)`` and
fills it with ~90 days of synthetic recovery metrics, activities, an in-progress
training plan and a few personal records; every later call just returns the same row.
Nothing here ever touches Garmin or Anthropic (see ``app.core.demo`` for the runtime
kill switch that also enforces that at the network layer).
"""
import datetime as dt
import random
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import hash_password_async
from app.db import users as users_db
from app.db.models import (
    ActivityRecord,
    DailyMetric,
    PersonalRecord,
    PlannedWorkout,
    TrainingPlan,
    User,
)

DEMO_EMAIL = "demo@garmin-coach.local"

_RNG_SEED = 20260101
_HISTORY_DAYS = 90

_PLAN_GOAL = "faster_5k"
_PLAN_GOAL_LABEL = "Швидше 5 км"
_PLAN_WEEKS_PAST = 3     # weeks already behind "today"
_PLAN_WEEKS_FUTURE = 5   # weeks still ahead
_RUN_DAYS = ["tue", "thu", "sat", "sun"]
_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def ensure_demo_user(session: AsyncSession) -> User:
    """Return the singleton demo account, creating and seeding it on first use."""
    existing = await users_db.get_by_email(session, DEMO_EMAIL)
    if existing is not None:
        return existing

    user = User(
        email=DEMO_EMAIL,
        # A random, never-shared password — the demo is only reachable via the
        # /demo-login button, never the normal password form.
        password_hash=await hash_password_async(secrets.token_hex(32)),
        is_admin=False,
        is_approved=True,
        is_active=True,
        is_demo=True,
        garmin_sync_enabled=False,
        plan_adapt_enabled=False,
        alerts_enabled=False,
    )
    session.add(user)
    await session.flush()  # assign user.id without a full commit yet

    rng = random.Random(_RNG_SEED)
    today = dt.date.today()
    _seed_daily_metrics(session, user.id, today, rng)
    activities_by_date = _seed_activities(session, user.id, today, rng)
    await session.flush()  # ActivityRecord rows need ids before PlannedWorkout can link them
    await _seed_plan(session, user.id, today, activities_by_date, rng)
    _seed_records(session, user.id, activities_by_date)

    await session.commit()
    await session.refresh(user)
    return user


def _seed_daily_metrics(session, user_id: int, today: dt.date, rng: random.Random) -> None:
    for i in range(_HISTORY_DAYS, -1, -1):
        d = today - dt.timedelta(days=i)
        sleep_h = round(rng.uniform(6.0, 8.2), 1)
        deep_h = round(sleep_h * rng.uniform(0.12, 0.20), 2)
        rem_h = round(sleep_h * rng.uniform(0.18, 0.24), 2)
        light_h = round(max(0.0, sleep_h - deep_h - rem_h - 0.3), 2)
        awake_h = round(max(0.0, sleep_h - deep_h - rem_h - light_h), 2)
        hrv = rng.randint(38, 72)
        stress_avg = rng.randint(15, 42)
        session.add(DailyMetric(
            user_id=user_id, date=d.isoformat(),
            sleep_score=rng.randint(58, 94),
            sleep_h=sleep_h, deep_h=deep_h, rem_h=rem_h, light_h=light_h, awake_h=awake_h,
            hrv_avg=hrv,
            hrv_status=rng.choices(
                ["balanced", "unbalanced", "low"], weights=[7, 2, 1]
            )[0],
            stress_avg=stress_avg, stress_max=min(99, stress_avg + rng.randint(15, 45)),
            bb_charged=rng.randint(65, 97), bb_drained=rng.randint(35, 72),
            extra={
                "resting_hr": rng.randint(44, 57),
                "spo2": rng.randint(95, 99),
                "vo2max": round(rng.uniform(46.0, 52.0), 1),
            },
        ))


def _seed_activities(session, user_id: int, today: dt.date, rng: random.Random) -> dict:
    """A handful of runs/cross-training over the history window. Returns {date: ActivityRecord}
    for the runs — the plan seeder links "done" sessions to these instead of inventing a
    second, disconnected set of dates."""
    by_date: dict = {}
    next_activity_id = 900_000_001  # any large id space clear of a real Garmin id
    d = today
    while d >= today - dt.timedelta(days=_HISTORY_DAYS):
        wd = d.weekday()  # Mon=0 .. Sun=6
        act = None
        if wd in (1, 3, 6) and rng.random() < 0.85:            # tue/thu/sun run days
            long_run = wd == 6
            dist = round(rng.uniform(8.5, 11.5) if long_run else rng.uniform(4.5, 7.5), 2)
            pace_s_per_km = rng.uniform(300, 345)  # 5:00–5:45/km
            dur_min = round(dist * pace_s_per_km / 60, 1)
            act = ActivityRecord(
                user_id=user_id, activity_id=next_activity_id, date=d.isoformat(),
                type="running", dur_min=dur_min, dist_km=dist,
                avg_hr=rng.randint(140, 158), max_hr=rng.randint(160, 178),
                load=round(rng.uniform(40, 110), 1),
            )
        elif wd == 5 and rng.random() < 0.4:                    # occasional Saturday strength
            act = ActivityRecord(
                user_id=user_id, activity_id=next_activity_id, date=d.isoformat(),
                type="strength_training", dur_min=round(rng.uniform(35, 55), 1),
                avg_hr=rng.randint(105, 125), max_hr=rng.randint(135, 155),
                load=round(rng.uniform(20, 45), 1),
            )
        if act is not None:
            next_activity_id += 1
            session.add(act)
            by_date[d.isoformat()] = act
        d -= dt.timedelta(days=1)

    # A recent run with a pre-written (seeded, never LLM-generated) analysis, so the
    # activity-detail page shows what that panel normally looks like.
    for iso in sorted(by_date, reverse=True):
        act = by_date[iso]
        if act.type == "running":
            act.analysis = (
                "🎭 Демонстраційний аналіз (не згенерований Claude): темп рівний, "
                "пульс у 2-й зоні — типова легка пробіжка на базовому фоні."
            )
            break
    return by_date


async def _seed_plan(session, user_id: int, today: dt.date, activities_by_date: dict,
                      rng: random.Random) -> None:
    monday = today - dt.timedelta(days=today.weekday())
    start_monday = monday - dt.timedelta(weeks=_PLAN_WEEKS_PAST)
    total_weeks = _PLAN_WEEKS_PAST + 1 + _PLAN_WEEKS_FUTURE
    target_date = monday + dt.timedelta(weeks=_PLAN_WEEKS_FUTURE, days=6)

    plan = TrainingPlan(
        user_id=user_id, goal=_PLAN_GOAL, goal_label=_PLAN_GOAL_LABEL,
        target_date=target_date.isoformat(), start_date=start_monday.isoformat(),
        days_per_week=len(_RUN_DAYS), intensity="moderate",
        intake={
            "run_days": _RUN_DAYS, "long_run_day": "sun", "adjust_level": "conservative",
        },
        summary=(
            "🎭 Демонстраційний план (не згенерований Claude): база + темпові відрізки "
            "у вівторок, легкий четвер, довгий біг у неділю — 8-тижнева підводка до "
            "прискорення на 5 км."
        ),
        status="active",
    )
    session.add(plan)
    await session.flush()

    _SESSION_TYPES = {
        "tue": ("tempo", "Темпові 6×800м @ 4:35/км, відновлення 90с підтюпцем", 8.0),
        "thu": ("easy", "Легкий біг у розмові", 6.0),
        "sat": ("easy", "Відновлювальний біг або відпочинок", 5.0),
        "sun": ("long", "Довгий біг у рівному темпі", 10.0),
    }

    for w in range(total_weeks):
        wk_monday = start_monday + dt.timedelta(weeks=w)
        for slug in _RUN_DAYS:
            date = wk_monday + dt.timedelta(days=_WEEKDAY_INDEX[slug])
            kind, desc, base_dist = _SESSION_TYPES[slug]
            iso = date.isoformat()
            workout = PlannedWorkout(
                plan_id=plan.id, user_id=user_id, date=iso, week=w + 1,
                type=kind, dist_km=round(base_dist + rng.uniform(-0.8, 0.8), 1),
                description=desc,
            )
            if date < today:
                match = activities_by_date.get(iso)
                if match is not None and match.type == "running":
                    workout.status = "done"
                    workout.completed_activity_id = match.id
                    workout.match_info = {
                        "actual_dist_km": match.dist_km,
                        "dist_delta_km": round((match.dist_km or 0) - workout.dist_km, 1),
                    }
                elif rng.random() < 0.75:
                    workout.status = "done"
                else:
                    workout.status = "missed"
            session.add(workout)


def _seed_records(session, user_id: int, activities_by_date: dict) -> None:
    runs = sorted(
        (a for a in activities_by_date.values() if a.type == "running" and a.dist_km),
        key=lambda a: a.date,
    )
    if not runs:
        return
    fastest = min(runs, key=lambda a: (a.dur_min or 1e9) / (a.dist_km or 1))
    longest = max(runs, key=lambda a: a.dist_km or 0)
    fastest_5k_min = (fastest.dur_min or 0) / (fastest.dist_km or 1) * 5
    session.add(PersonalRecord(
        user_id=user_id, kind="fastest_5k", value=round(fastest_5k_min, 2),
        activity_id=fastest.id, date=fastest.date,
    ))
    session.add(PersonalRecord(
        user_id=user_id, kind="longest_run", value=longest.dist_km,
        activity_id=longest.id, date=longest.date,
    ))
