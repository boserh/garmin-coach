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
import re
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


async def _backfill_records(email: str) -> int:
    """Seed the personal_records table from the user's full stored history (EP-14).
    Idempotent and SILENT: it runs the same detector the bot uses, but sends no
    celebrations — records are dated in the past, so nothing is 'fresh'. Run once after
    importing years of history; the daily tick keeps it current afterwards."""
    from app import records

    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            print(f"User {email} not found.")
            return 1
        before = len(await records.current_records(session, user.id))
        new = await records.detect_records(session, user.id)
        await session.commit()
        if not new:
            print(f"No new records (already have {before}).")
            return 0
        print(f"Recorded {len(new)} personal best(s) for {email}:")
        for r in sorted(new, key=lambda x: records.DISPLAY_ORDER.index(x.kind)
                        if x.kind in records.DISPLAY_ORDER else 99):
            prev = (f" (was {records.format_value(r.kind, r.previous_value)})"
                    if r.previous_value is not None else "")
            print(f"  {records.LABELS.get(r.kind, r.kind)}: "
                  f"{records.format_value(r.kind, r.value)}{prev}  [{r.date}]")
    return 0


async def _backfill_strength_snapshots(email: str) -> int:
    """ST-09: fill in null strength_snapshot on the active plan's clone days (a
    garmin_template_id set but the snapshot never got written — the pre-fix symptom was a
    garth client that never logged in before the strength fetch, so it silently degraded to
    {}/[]). Idempotent: only rows whose snapshot is missing/empty are touched (the JSON-null
    gotcha — filter in Python, not `.is_(None)`), so a repeat run is a no-op. Fetches each
    distinct template once, live, under a bound + logged-in Garmin session."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import client, repository, workout_export
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
        ws = await repository.list_workouts(session, plan.id)
        todo = [
            w for w in ws
            if w.type == "strength" and w.garmin_template_id
            and not (isinstance(w.strength_snapshot, dict) and w.strength_snapshot.get("exercises"))
        ]
        if not todo:
            print("Nothing to backfill (all clone-day snapshots already filled).")
            return 0

        print(f"Backfilling {len(todo)} strength snapshot(s) for {email}...")
        async with user_runtime(session, user):
            await run_in_threadpool(get_provider().login)
            cache: dict = {}
            for w in todo:
                tid = w.garmin_template_id
                if tid not in cache:
                    raw = await run_in_threadpool(client.fetch_workout_full, tid)
                    if raw:
                        cache[tid] = {
                            "name": (raw.get("workoutName") or "").strip() or None,
                            "exercises": workout_export.read_exercises(raw),
                        }
                    else:
                        cache[tid] = None
                        print(f"  tid={tid}: empty fetch, skipped")
                    await asyncio.sleep(0.3)  # be gentle on Garmin
                snap = cache[tid]
                if snap and snap.get("exercises"):
                    w.strength_snapshot = snap
                    print(f"  {w.date}  {snap.get('name') or 'Силова'}"
                          f"  ({len(snap['exercises'])} exercise(s))")
        done = sum(1 for w in todo if isinstance(w.strength_snapshot, dict)
                   and w.strength_snapshot.get("exercises"))
        await session.commit()
        print(f"Done: {done}/{len(todo)} snapshots filled.")
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


def _pace_hint(pace) -> str:
    """[fast, slow] decimal min/km → 'орієнтовно 6:45–7:00/км' — the text stashed in a
    step's ``note`` when its pace target is replaced by an HR zone, so the range is still
    visible on the watch (as the step description) instead of vanishing."""
    def fmt(dec):
        total = round(dec * 60)
        return f"{total // 60}:{total % 60:02d}"
    return f"орієнтовно {fmt(pace[0])}–{fmt(pace[1])}/км"


def _convert_easy_steps(steps, zone: int):
    """Return ``(new_steps, n_changed)``: every ``run`` step that carries a pace range is
    rewritten to target a heart-rate zone instead (drops ``pace_min_km``, sets ``hr_zone``,
    and — unless the step already has a ``note`` — stashes the old pace range as text via
    ``_pace_hint`` so it still shows on the watch as the step's description). Recurses into
    ``repeat`` groups; leaves warmup/cooldown/recovery/no-target steps alone. Pure — builds a
    fresh list (JSON columns need a reassignment to be marked dirty)."""
    out, changed = [], 0
    for s in steps or []:
        if not isinstance(s, dict):
            out.append(s)
            continue
        s = dict(s)
        if s.get("kind") == "repeat":
            s["steps"], c = _convert_easy_steps(s.get("steps"), zone)
            changed += c
        elif s.get("kind") == "run" and s.get("pace_min_km") is not None:
            pace = s.pop("pace_min_km", None)
            s["hr_zone"] = zone
            if not s.get("note") and isinstance(pace, (list, tuple)) and len(pace) == 2:
                s["note"] = _pace_hint(pace)
            changed += 1
        out.append(s)
    return out, changed


# A pace range like "4:50–5:10" (en/em dash or hyphen) inside a workout description.
_PACE_RANGE_RE = re.compile(r"(\d):([0-5]\d)\s*[–—-]\s*(\d):([0-5]\d)")

# A "stride"/acceleration is a SHORT fast rep; anything longer is a real interval, not a
# stride, and is left alone.
_STRIDE_MAX_M = 400


def _parse_pace_ranges(text: str):
    """All 'm:ss–m:ss' pace ranges in the text as (fast, slow) decimal min/km tuples."""
    out = []
    for m in _PACE_RANGE_RE.finditer(text or ""):
        a = int(m.group(1)) + int(m.group(2)) / 60
        b = int(m.group(3)) + int(m.group(4)) / 60
        out.append((min(a, b), max(a, b)))
    return out


def _stride_pace_from_desc(desc: str):
    """The stride pace target [fast, slow] parsed from a description, or None. A description
    with strides names TWO paces — the easy running pace and the (faster) stride pace, e.g.
    'легкий 2.4 км у темпі 6:55–7:20/км, потім 4 прискорення (темп ~4:50–5:10/км)'. We take
    the FASTEST range, but only when there's a clear easy-vs-fast gap (≥0.75 min/km) — so a
    plain easy run with one pace range is never mistaken for having strides."""
    ranges = _parse_pace_ranges(desc)
    if len(ranges) < 2:
        return None
    fastest = min(ranges, key=lambda r: r[0])
    slowest = max(ranges, key=lambda r: r[1])
    if slowest[1] - fastest[0] < 0.75:
        return None
    return [round(fastest[0], 3), round(fastest[1], 3)]


def _strides_to_pace(steps, pace, *, in_repeat: bool = False):
    """Return ``(new_steps, n_changed)``: every SHORT ``run`` step inside a ``repeat`` group
    that currently targets an HR zone (a mis-classified stride) is rewritten to target
    ``pace`` instead (drops ``hr_zone``/``note``, sets ``pace_min_km``). HR lags too much to
    govern a 100 m stride — it needs a pace. Only touches run steps nested in a repeat (the
    stride signature); the steady easy leg and warmup/cooldown are left alone. Pure — builds
    a fresh list (JSON columns need reassignment to be marked dirty)."""
    out, changed = [], 0
    for s in steps or []:
        if not isinstance(s, dict):
            out.append(s)
            continue
        s = dict(s)
        if s.get("kind") == "repeat":
            s["steps"], c = _strides_to_pace(s.get("steps"), pace, in_repeat=True)
            changed += c
        elif (in_repeat and s.get("kind") == "run" and s.get("hr_zone") is not None
              and s.get("pace_min_km") is None
              and isinstance(s.get("dist_m"), (int, float)) and s["dist_m"] <= _STRIDE_MAX_M):
            s.pop("hr_zone", None)
            s.pop("note", None)  # the note carried the easy hint; pace is now explicit
            s["pace_min_km"] = list(pace)
            changed += 1
        out.append(s)
    return out, changed


async def _fix_stride_paces(email: str, dry_run: bool, no_sync: bool) -> int:
    """One-off data fix: give the active plan's strides/accelerations a PACE target instead of
    the HR zone they were mis-generated with (before the SYSTEM_PLAN fix). The stride pace is
    parsed from each session's own description (the faster of its two pace ranges); sessions
    without a clear stride pace are skipped and reported (never guessed). Then re-push the
    upcoming in-window sessions to Garmin (``plan_sync.resync_workouts``). ``--dry-run``
    previews without writing; ``--no-sync`` updates the DB but leaves the watch untouched."""
    import datetime as dt

    from app.garmin import plan_sync, repository
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

        today = dt.date.today().isoformat()
        end = (dt.date.today() + dt.timedelta(days=14)).isoformat()

        changed = []    # (workout, n, new_steps, pace) — need a DB rewrite
        skipped = []    # (workout,) — had strides on HR zone but no parseable stride pace
        to_sync = []    # touched upcoming in-window sessions to mirror to Garmin
        for w in await repository.list_workouts(session, plan.id):
            pace = _stride_pace_from_desc(w.description or "")
            if pace is None:
                # flag a session that HAS stride-shaped steps but no pace we could recover
                probe, n = _strides_to_pace(w.steps, [0, 0])
                if n:
                    skipped.append(w)
                continue
            new_steps, n = _strides_to_pace(w.steps, pace)
            if n:
                changed.append((w, n, new_steps, pace))
                if w.status == "planned" and today <= w.date <= end:
                    to_sync.append(w)

        if not changed and not skipped:
            print("Nothing to do — no strides on HR zones in this plan.")
            return 0

        for w, n, _new, pace in changed:
            print(f"  {'[dry-run] ' if dry_run else ''}{w.date}  {w.type:9s} "
                  f"{n} stride(s) → {_pace_hint(pace)}")
        for w in skipped:
            print(f"  [skip] {w.date}  {w.type:9s} strides on HR zone but no stride pace in "
                  "description — fix by hand or regenerate")

        if dry_run:
            print(f"[dry-run] would rewrite {len(changed)} session(s) and re-sync "
                  f"{len(to_sync)} in-window one(s) to Garmin. No writes made.")
            return 0

        if not changed:
            print("No sessions rewritten.")
            return 0

        for w, _n, new_steps, _pace in changed:
            w.steps = new_steps
        await session.commit()
        print(f"DB updated: {len(changed)} session(s) now give strides a pace.")

        if no_sync:
            print(f"--no-sync: Garmin calendar left untouched ({len(to_sync)} in-window "
                  "session(s) not re-synced).")
            return 0
        if not to_sync:
            print("No upcoming in-window sessions to re-sync to Garmin.")
            return 0
        async with user_runtime(session, user):
            res = await plan_sync.resync_workouts(session, user.id, to_sync)
        print(f"Garmin re-synced: +{res['pushed']} pushed, -{res['removed']} removed.")
    return 0


async def _convert_easy_hr(email: str, easy_zone: int, recovery_zone: int, long_zone: int,
                           dry_run: bool, no_sync: bool) -> int:
    """One-off migration: rewrite the active plan's easy/recovery/long run steps from a pace
    range to a heart-rate-zone target (easy → ``--easy-zone``, recovery → ``--recovery-zone``,
    long → ``--long-zone``), stashing the old pace range as the step's ``note`` (shows on the
    watch as the step description), then re-push the upcoming in-window sessions to Garmin
    (drop the old pace-based copy, push the HR-zone one via ``plan_sync.resync_workouts``).
    Past/out-of-window sessions get the DB rewrite only. ``--dry-run`` previews (incl. the
    built Garmin target) without writing; ``--no-sync`` updates the DB but leaves the watch
    untouched.

    NB the ``heart.rate.zone`` DTO is not yet verified field-for-field against a real saved
    Garmin workout — dry-run it first and eyeball the target before a live push."""
    import datetime as dt

    from app.garmin import plan_sync, repository, workout_export
    from app.garmin.runtime import user_runtime

    zones = {"easy": easy_zone, "recovery": recovery_zone, "long": long_zone}

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

        today = dt.date.today().isoformat()
        end = (dt.date.today() + dt.timedelta(days=14)).isoformat()

        changed = []   # (workout, n_steps_changed, new_steps) — need a DB rewrite this run
        to_sync = []   # easy/recovery/long sessions we mirror to Garmin (upcoming, in-window)
        for w in await repository.list_workouts(session, plan.id):
            zone = zones.get((w.type or "").lower())
            if zone is None:
                continue
            new_steps, n = _convert_easy_steps(w.steps, zone)
            if n:
                changed.append((w, n, new_steps))
            # re-sync independently of a DB change this run: a plan already converted with
            # --no-sync has nothing to rewrite but still needs its Garmin copy refreshed.
            # Only upcoming in-window planned sessions — never strip past/completed ones.
            if w.status == "planned" and today <= w.date <= end:
                to_sync.append(w)

        if not changed and not to_sync:
            print("Nothing to do — no easy/recovery/long sessions to convert or re-sync.")
            return 0

        if changed:
            print(f"{'[dry-run] ' if dry_run else ''}Converting {len(changed)} easy/recovery/long "
                  f"session(s) for {email} (easy→zone {easy_zone}, recovery→zone {recovery_zone}, "
                  f"long→zone {long_zone}):")
            for w, n, new_steps in changed:
                zone = zones[(w.type or "").lower()]
                print(f"  {w.date}  {w.type:9s} {n} step(s) → пульс зона {zone}")
        else:
            print("DB already on HR zones — nothing to rewrite.")

        if dry_run:
            # show the built Garmin target for the first still-upcoming session
            sample = next((c for c in changed if c[0].date >= today), None) or (
                (changed[0] if changed else None))
            if sample:
                w, _, new_steps = sample
                w.steps = new_steps  # in-memory only; session is never committed on dry-run
                payload = workout_export.build_workout(w)
                targets = [st.get("targetType", {}).get("workoutTargetTypeKey")
                           for st in payload["workoutSegments"][0]["workoutSteps"]]
                print(f"\n[dry-run] sample push payload for {w.date} ({w.type}): "
                      f"targets={targets}")
            print(f"[dry-run] would re-sync {len(to_sync)} in-window session(s) to Garmin. "
                  "No DB or Garmin writes made.")
            return 0

        if changed:
            for w, _, new_steps in changed:
                w.steps = new_steps
            await session.commit()
            print(f"DB updated: {len(changed)} session(s) now target HR zones.")

        if no_sync:
            print(f"--no-sync: Garmin calendar left untouched ({len(to_sync)} in-window "
                  "session(s) not re-synced — run again without --no-sync to push them).")
            return 0

        if not to_sync:
            print("No upcoming in-window sessions to re-sync to Garmin.")
            return 0

        async with user_runtime(session, user):
            res = await plan_sync.resync_workouts(session, user.id, to_sync)
        print(f"Garmin re-synced: +{res['pushed']} pushed, -{res['removed']} removed "
              "(upcoming in-window sessions only).")
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

    ceh = sub.add_parser(
        "convert-easy-hr",
        help="Rewrite active-plan easy/recovery/long runs from pace to HR-zone + re-push to Garmin")
    ceh.add_argument("--email", required=True)
    ceh.add_argument("--easy-zone", type=int, default=2, help="HR zone for easy runs (default 2)")
    ceh.add_argument("--recovery-zone", type=int, default=2,
                     help="HR zone for recovery runs (default 2)")
    ceh.add_argument("--long-zone", type=int, default=2, help="HR zone for long runs (default 2)")
    ceh.add_argument("--dry-run", action="store_true",
                     help="preview the conversion + sample Garmin target, don't write")
    ceh.add_argument("--no-sync", action="store_true",
                     help="update the DB only, leave the Garmin calendar untouched")

    fsp = sub.add_parser(
        "fix-stride-paces",
        help="Give active-plan strides a pace target (not HR zone) + re-push to Garmin")
    fsp.add_argument("--email", required=True)
    fsp.add_argument("--dry-run", action="store_true",
                     help="preview which strides get which pace, don't write")
    fsp.add_argument("--no-sync", action="store_true",
                     help="update the DB only, leave the Garmin calendar untouched")

    lw = sub.add_parser("list-workouts", help="List the user's saved Garmin workouts (id/name)")
    lw.add_argument("--email", required=True)

    br = sub.add_parser(
        "backfill-records", help="Seed personal records from stored history (silent, EP-14)")
    br.add_argument("--email", required=True)

    bss = sub.add_parser(
        "backfill-strength-snapshots",
        help="Fill null strength_snapshot on the active plan's clone days (ST-09)")
    bss.add_argument("--email", required=True)

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
    if args.cmd == "convert-easy-hr":
        return asyncio.run(_convert_easy_hr(
            args.email, args.easy_zone, args.recovery_zone, args.long_zone,
            args.dry_run, args.no_sync))
    if args.cmd == "fix-stride-paces":
        return asyncio.run(_fix_stride_paces(args.email, args.dry_run, args.no_sync))
    if args.cmd == "list-workouts":
        return asyncio.run(_list_workouts(args.email))
    if args.cmd == "backfill-records":
        return asyncio.run(_backfill_records(args.email))
    if args.cmd == "backfill-strength-snapshots":
        return asyncio.run(_backfill_strength_snapshots(args.email))
    if args.cmd == "token-expiry":
        return asyncio.run(_token_expiry())
    return 0


if __name__ == "__main__":
    sys.exit(main())
