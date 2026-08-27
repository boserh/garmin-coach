"""Command-line admin tasks.

Run with the venv interpreter, e.g. create the first (admin) account and seed its
Garmin/Claude/Telegram credentials from the existing ``.env``::

    ./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env

``--seed-env`` requires ``APP_SECRET_KEY`` (the creds are encrypted at rest). Add
``--backfill-month`` to immediately pull the last 30 days of Garmin activities/daily
data for the new user (real Garmin API calls — no Anthropic cost, but needs the
account's Garmin creds present, e.g. via ``--seed-env``).
"""
import argparse
import asyncio
import contextlib
import getpass
import pathlib
import sys

from sqlalchemy import text

from app.core.config import settings
from app.core.crypto import encrypt, hash_password
from app.db import users
from app.db.base import async_session_maker, init_db


class _UserNotFound(Exception):
    """Raised by ``cli_user`` when ``--email`` resolves to no user; ``_run`` in ``main``
    turns it into the uniform 'User <email> not found.' message + exit code 1, so no
    command needs to repeat that check."""

    def __init__(self, email: str):
        self.email = email


@contextlib.asynccontextmanager
async def cli_user(email: str, *, garmin: bool = False):
    """Shared CLI preamble (A4): init the DB, open a session, resolve the user by ``email``
    (raising :class:`_UserNotFound` when missing), and yield ``(session, user)``. With
    ``garmin=True`` it also binds the user's Garmin runtime and logs in **up front** — only
    for commands that always need a live session (e.g. ``list-workouts``); commands that defer
    the login past a 'nothing to do' / ``--dry-run`` check keep ``garmin=False`` and use
    :func:`garmin_login` mid-body instead, so a no-op or dry run never touches Garmin."""
    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            raise _UserNotFound(email)
        if garmin:
            async with garmin_login(session, user):
                yield session, user
        else:
            yield session, user


@contextlib.asynccontextmanager
async def garmin_login(session, user):
    """The deferred Garmin login several commands run *after* their 'is there work / dry-run'
    checks (so a no-op or ``--dry-run`` never logs in): bind the user's runtime and perform the
    login. Factored out as the single place the future OPS-01 auth migration has to patch."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin.providers import get_provider
    from app.garmin.runtime import user_runtime

    async with user_runtime(session, user):
        await run_in_threadpool(get_provider().login)
        yield


def _run(coro) -> int:
    """Run a command coroutine, turning :class:`_UserNotFound` into the uniform message +
    exit 1 so every command's dispatch shares one not-found path."""
    try:
        return asyncio.run(coro)
    except _UserNotFound as e:
        print(f"User {e.email} not found.")
        return 1


async def _import_garth_token(email: str, path: str) -> int:
    """Seed a user's session from a garth token dir. Only useful with the OPS-10
    rollback (``GARMIN_PROVIDER=garth`` + the ``garth`` extra installed): the native
    engine can't read a garth blob and will just do one silent fresh login instead."""
    if settings.GARMIN_PROVIDER.lower() != "garth":
        print("Note: GARMIN_PROVIDER is not 'garth' — the imported token will be "
              "ignored by the native engine (one fresh login on next use).")
    garth_dir = pathlib.Path(path).expanduser()
    if not garth_dir.exists():
        print(f"{garth_dir} not found.")
        return 1
    try:
        import garth
        garth.resume(str(garth_dir))
        token = garth.client.dumps()
    except Exception as e:
        print(f"Failed to read garth token: {e}")
        return 1
    async with cli_user(email) as (session, user):
        user.garth_token_enc = encrypt(token)
        await session.commit()
        print(f"Garth token imported for {email}.")
    return 0


async def _import_fit_series(email: str, path: str, since: str) -> int:
    """Backfill runs' pace/HR series from the export's FIT files (offline, no API)."""
    from app.garmin.export_import import import_fit_series

    async with cli_user(email) as (session, user):
        stats = await import_fit_series(session, user.id, path, since=since)
    if stats.get("error"):
        print(stats["error"])
        return 1
    print(f"Added pace/HR series to {stats['series_added']}/{stats['runs']} run(s).")
    return 0


async def _import_export(email: str, path: str, overwrite: bool, since: str) -> int:
    """Backfill daily_metrics from a Garmin GDPR export folder (offline, no API)."""
    from app.garmin.export_import import import_export

    async with cli_user(email) as (session, user):
        stats = await import_export(session, user.id, path, overwrite=overwrite, since=since)
    print(f"Inserted {stats['inserted']} new day(s); filled {stats['filled']} existing; "
          f"{stats['unchanged']} unchanged ({stats['parsed']} parsed).")
    return 0


async def _backfill_series(email: str, since: str, force: bool = False) -> int:
    """Fetch the pace/HR series for this user's already-stored runs that don't have
    one yet (saved before the feature existed, or imported from the export). Idempotent —
    only fills nulls. ``since`` (ISO) limits to recent runs so it isn't hundreds of API
    calls at once.

    ``--force`` (NF-25) instead REFETCHES runs that already have a series, to pick up the
    channels a newer key version added (cadence/GCT/vertical oscillation, coordinates). It
    is opt-in for a reason: history stays perfectly usable without it (every consumer treats
    a missing channel as absent, not as zero), so this is a deliberate "I want the new
    numbers on my old runs" pass, ideally with ``--since`` to bound it. Zero LLM cost, but
    one Garmin request per run."""
    import asyncio

    from fastapi.concurrency import run_in_threadpool
    from sqlalchemy import select

    from app.db.models import ActivityRecord
    from app.garmin import client

    async with cli_user(email) as (session, user):
        # JSON None is stored as JSON `null` (not SQL NULL), so filter in Python.
        stmt = select(ActivityRecord).where(
            ActivityRecord.user_id == user.id,
            ActivityRecord.type.like("%run%"),
        )
        if since:
            stmt = stmt.where(ActivityRecord.date >= since)
        rows = [r for r in (await session.execute(
            stmt.order_by(ActivityRecord.date.desc()))).scalars().all()
            if force or not r.series]
        if not rows:
            print("No runs need backfilling.")
            return 0
        print(f"Backfilling {len(rows)} run(s) for {email}...")
        done = 0
        async with garmin_login(session, user):
            for r in rows:
                sr = await run_in_threadpool(
                    client.fetch_activity_series, r.activity_id, force=force)
                if sr:
                    r.series = sr
                    done += 1
                    print(f"  {r.date} {r.type} (id={r.activity_id}) — {len(sr)} pts")
                await asyncio.sleep(0.3)  # be gentle on Garmin
            await session.commit()
        print(f"Done: {done}/{len(rows)} updated.")
    return 0


async def _backfill_routes(email: str, since: str = "") -> int:
    """NF-33: cluster this user's already-stored runs into recognised routes.

    Zero Garmin calls and zero LLM cost — it only reads the coordinates already in each
    stored ``series`` and runs the pure matcher. Idempotent by construction: an activity that
    already carries a ``route_id`` is skipped and matching takes the first similar cluster, so
    running it twice never duplicates routes or re-partitions history (an AC). Runs without
    coordinates (treadmill, pre-NF-33 series) are simply left unassigned."""
    from sqlalchemy import select

    from app.db.models import ActivityRecord
    from app.garmin.repository import routes as routes_repo

    async with cli_user(email) as (session, user):
        stmt = select(ActivityRecord).where(
            ActivityRecord.user_id == user.id,
            ActivityRecord.route_id.is_(None),
            ActivityRecord.is_hidden.is_(False),
        )
        if since:
            stmt = stmt.where(ActivityRecord.date >= since)
        rows = [r for r in (await session.execute(
            stmt.order_by(ActivityRecord.date))).scalars().all() if r.series]
        if not rows:
            print("No activities need route clustering.")
            return 0
        print(f"Clustering {len(rows)} activity(ies) for {email}...")
        linked = 0
        for r in rows:
            route_id = await routes_repo.assign_route(session, user.id, r)
            if route_id is not None:
                linked += 1
        await session.commit()
        total = len(await routes_repo.list_routes(session, user.id))
        print(f"Done: {linked}/{len(rows)} linked, {total} route(s) known.")
    return 0


async def _backfill_zones(email: str, days: int) -> int:
    """NF-24: fetch HR time-in-zone for this user's stored activities that don't have it yet.

    Idempotent (only fills rows whose ``zones`` carry no per-zone seconds — a row that only
    has training effect from the list DTO still needs the zone fetch) and paced, so a year
    of history doesn't turn into a burst against Garmin's unofficial API. Zero LLM cost."""
    import asyncio
    import datetime as dt

    from fastapi.concurrency import run_in_threadpool
    from sqlalchemy import select

    from app.db.models import ActivityRecord
    from app.garmin import client

    def _has_zone_time(z) -> bool:
        return isinstance(z, dict) and any(f"z{n}_s" in z for n in range(1, 6))

    async with cli_user(email) as (session, user):
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = (await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.date >= cutoff,
                ActivityRecord.avg_hr.is_not(None),   # no HR → zones can't exist
            ).order_by(ActivityRecord.date.desc())
        )).scalars().all()
        rows = [r for r in rows if not _has_zone_time(r.zones)]
        if not rows:
            print("No activities need zone backfilling.")
            return 0
        print(f"Backfilling zones for {len(rows)} activity(ies) of {email}...")
        done = 0
        async with garmin_login(session, user):
            for r in rows:
                z = await run_in_threadpool(client.fetch_activity_zones, r.activity_id)
                if z:
                    r.zones = {**(r.zones or {}), **z}
                    done += 1
                    total_min = round(sum(z.values()) / 60)
                    print(f"  {r.date} {r.type} (id={r.activity_id}) — {total_min} min in zones")
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
    from app.garmin.service import _auto_activities

    async with cli_user(email) as (session, user):
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
        async with garmin_login(session, user):
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
    """Seed the personal_records table from the user's full stored history (EP-14 + NF-27).
    Idempotent and SILENT: it runs the same detector the bot uses, but sends no
    celebrations — records are dated in the past, so nothing is 'fresh'. Run once after
    importing years of history; the daily tick keeps it current afterwards.

    NF-27 needs no backfill command of its own: tonnage and e1RM are DERIVED at read time
    from the executed sets already stored (``exercise:v3``), so the only thing to seed is
    the strength records — which this detector now produces alongside the running ones,
    at zero LLM and zero Garmin cost."""
    from app import records

    async with cli_user(email) as (session, user):
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
            print(f"  {records.label_for(r.kind)}: "
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

    async with cli_user(email) as (session, user):
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
        async with garmin_login(session, user):
            cache: dict = {}
            for w in todo:
                tid = w.garmin_template_id
                if tid not in cache:
                    raw = await run_in_threadpool(client.fetch_workout_full, tid)
                    if raw:
                        cache[tid] = {
                            "name": (raw.get("workoutName") or "").strip() or None,
                            "exercises": workout_export.read_exercises(raw),
                            "blocks": workout_export.read_blocks(raw),
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

    from app.garmin import plan_sync, repository, workout_export

    async with cli_user(email) as (session, user):
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        # Reuse plan_sync's forward selection (window/pushable/skip-already-pushed) — the
        # CLI adds only its own extras: --dry-run and ignoring garmin_sync_enabled.
        todo = await plan_sync.select_forward(session, plan.id, days=days, only_date=date)
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

        async with garmin_login(session, user):
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
    from app.garmin import plan_sync, repository

    async with cli_user(email) as (session, user):
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        pushed = await plan_sync.select_pushed(session, plan.id, only_date=date)
        if not pushed:
            print("Nothing pushed for this plan.")
            return 0
        print(f"Removing {len(pushed)} pushed workout(s) for {email}...")
        async with garmin_login(session, user):
            for w in pushed:
                wid = w.garmin_workout_id
                if await plan_sync.remove_workout(session, w):
                    print(f"  {w.date}  removed workout {wid}")
                else:
                    print(f"  {w.date}  workout {wid} already gone")
                await asyncio.sleep(0.3)
        print("Done.")
    return 0


async def _fix_plan_steps(email: str, apply: bool) -> int:
    """Repair planned sessions whose headline ``dist_km`` and structured ``steps`` disagree
    — rows written before the write path reconciled them (an adaptation that eased only the
    distance left the original steps in place, so the header said 5 km while the workout on
    the watch was still 6 km).

    Pure DB: 0 Claude calls, 0 Garmin requests. Read-only unless ``--apply``. The stored
    ``dist_km`` is the coach's intent, so the steps are re-cut to it (``app.plansteps``).
    Sessions already pushed to Garmin still carry the OLD workout — the printed list tells
    you which; ``unpush-plan`` + ``push-plan`` re-pushes them."""
    from app import plansteps
    from app.garmin import repository

    async with cli_user(email) as (session, user):
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        fixed = pushed = 0
        for w in await repository.list_workouts(session, plan.id):
            gap = plansteps.mismatch(w.dist_km, w.steps)
            if gap is None or gap <= plansteps.TOLERANCE:
                continue
            was = plansteps.total_dist_m(w.steps) or 0.0
            steps = plansteps.scale_steps(w.steps, w.dist_km)
            if steps is None:
                continue
            fixed += 1
            note = ""
            if w.garmin_workout_id is not None:
                pushed += 1
                note = "  [pushed to Garmin — needs a re-push]"
            print(f"  {w.date} {w.type or '':<10} dist_km={w.dist_km} "
                  f"steps {was:.0f}m → {plansteps.total_dist_m(steps):.0f}m{note}")
            if apply:
                w.steps = steps
        if apply:
            await session.commit()
        if not fixed:
            print("Nothing to fix — every session's steps match its distance.")
            return 0
        print(f"{'Fixed' if apply else 'Would fix'} {fixed} session(s).")
        if not apply:
            print("Re-run with --apply to write the changes.")
        elif pushed:
            print(f"{pushed} of them are already on the Garmin calendar with the old "
                  f"workout — re-push with:\n"
                  f"  python -m app.cli unpush-plan --email {email}\n"
                  f"  python -m app.cli push-plan --email {email}")
    return 0


async def _fix_plan_kinds(email: str, apply: bool, repush: bool) -> int:
    """Repair planned sessions that carry columns their ``type`` cannot own — a run day left
    holding a strength template (``garmin_template_id``/``strength_plan``), or a strength day
    left holding a run's ``dist_km``/``steps``.

    That is what a plan edit that swapped a run day with a strength day used to leave behind
    (see ``app.plankind``): on the watch the run appeared as a cloned "Day 1" under the run's
    own description, and ``/plan`` showed the strength session with a distance. The write path
    no longer produces it; this cleans up rows written before the fix.

    Pure DB by default: 0 Claude calls, 0 Garmin requests, read-only without ``--apply``.
    ``--apply --repush`` additionally repairs the **Garmin calendar** — the cleaned sessions
    that were already pushed still sit there as the wrong workout, so each is deleted and
    re-pushed from the corrected row (real Garmin calls, still no LLM cost)."""
    from app import plankind
    from app.garmin import plan_sync, repository

    async with cli_user(email) as (session, user):
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        broken = []
        for w in await repository.list_workouts(session, plan.id):
            cols = plankind.foreign_columns(w)
            if not cols:
                continue
            broken.append(w)
            note = "  [pushed to Garmin]" if w.garmin_workout_id is not None else ""
            print(f"  {w.date} {w.type or '':<10} drop {', '.join(cols)}{note}")
        if not broken:
            print("Nothing to fix — every session matches its type.")
            return 0
        if not apply:
            print(f"Would fix {len(broken)} session(s). "
                  f"Re-run with --apply to write the changes.")
            return 0
        for w in broken:
            plankind.reconcile(w)
        await session.commit()
        print(f"Fixed {len(broken)} session(s).")

        pushed = [w for w in broken if w.garmin_workout_id is not None]
        if not pushed:
            return 0
        if not repush:
            print(f"{len(pushed)} of them are on the Garmin calendar as the WRONG workout. "
                  f"Re-push with --repush, or by hand:\n"
                  f"  python -m app.cli unpush-plan --email {email}\n"
                  f"  python -m app.cli push-plan --email {email}")
            return 0
        print(f"Re-pushing {len(pushed)} session(s) to the Garmin calendar...")
        async with garmin_login(session, user):
            # resync_workouts is the per-edit path: drop our old copy, re-push the corrected
            # row when it is still an upcoming in-window session (a past one just goes).
            res = await plan_sync.resync_workouts(session, user.id, pushed)
        print(f"Garmin: -{res['removed']} removed, +{res['pushed']} re-pushed.")
    return 0


async def _backfill_activity_analysis(email: str, apply: bool) -> int:
    """Re-attach activity analyses that were generated but never stored on the row.

    Until the ``_activity_watch_for_user`` fix, the morning tick set ``activity.analysis``
    and committed nothing after it, so the text was rolled back when the session closed —
    the DM went out, the activity page stayed empty. Nothing was actually lost: every call
    is logged with its text in ``report_logs`` (``kind="activity"``, ``question`` carrying
    the row id), and that write commits. This copies the newest logged text back onto any
    activity still missing one.

    Pure DB: 0 Claude calls, 0 Garmin requests. Read-only unless ``--apply``. Never
    overwrites an analysis that is already there."""
    import re

    from sqlalchemy import select

    from app.db.models import ActivityRecord, ReportLog

    async with cli_user(email) as (session, user):
        logged: dict[int, str] = {}
        rows = await session.execute(
            select(ReportLog.question, ReportLog.report_text)
            .where(ReportLog.user_id == user.id, ReportLog.kind == "activity",
                   ReportLog.ok.is_(True), ReportLog.report_text.is_not(None))
            .order_by(ReportLog.created_at)          # newest wins
        )
        for question, text_ in rows:
            m = re.match(r"activity #(\d+)", question or "")
            if m:
                logged[int(m.group(1))] = text_

        if not logged:
            print("No activity analyses logged for this user — nothing to restore.")
            return 0

        acts = (await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user.id,
                ActivityRecord.id.in_(logged),
                ActivityRecord.analysis.is_(None),
            ).order_by(ActivityRecord.date)
        )).scalars().all()

        for act in acts:
            print(f"  #{act.id}  {act.date}  {act.type or '':<12} "
                  f"{len(logged[act.id])} chars")
            if apply:
                act.analysis = logged[act.id]
        if apply:
            await session.commit()

        if not acts:
            print(f"All {len(logged)} logged analyses are already on their activities.")
            return 0
        print(f"{'Restored' if apply else 'Would restore'} {len(acts)} of "
              f"{len(logged)} logged analyses.")
        if not apply:
            print("Re-run with --apply to write them.")
    return 0


async def _trigger_plan_adapt(email: str) -> int:
    """Manually run the weekly plan-adaptation review (EP-02) for one user, outside
    Sunday's schedule — same call plan_adapt_job makes. Pure-DB + one Claude call, no
    Garmin login needed. When it proposes a change, sends the normal confirm/reject
    proposal to the user's Telegram chat (the review itself is console-triggered; the
    ✅/❌ still happens in the bot, same as always) via a standalone Bot instance."""
    from types import SimpleNamespace

    from telegram import Bot

    from app.analysis.service import AnalystError, run_plan_adaptation
    from app.garmin import repository
    from app.garmin.credentials import load_credentials
    from bot.jobs import _send_adapt_proposal

    async with cli_user(email) as (session, user):
        if not user.telegram_chat_id:
            print("User has no telegram_chat_id — nowhere to send a proposal.")
            return 1
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            print("No active plan for this user.")
            return 1
        creds = load_credentials(user)
        if not creds.anthropic_key:
            print("No Anthropic key configured for this user.")
            return 1
        try:
            _plan, edit = await run_plan_adaptation(
                session, user_id=user.id, api_key=creds.anthropic_key, trigger="weekly",
            )
        except AnalystError as e:
            print(f"Adaptation call failed: {e}")
            return 1
        if edit is None:
            print("adjust_level=off for this plan — adaptation is disabled, no call made.")
            return 0
        if not edit.operations:
            print("Plan looks fine — nothing to propose.")
            return 0
        async with Bot(token=settings.TELEGRAM_BOT_TOKEN) as bot:
            await _send_adapt_proposal(
                SimpleNamespace(bot=bot), session, user, plan.id, edit)
        print(f"Proposal sent to {email}'s Telegram chat ({len(edit.operations)} op(s)).")
    return 0


async def _list_workouts(email: str) -> int:
    """Print the user's saved Garmin workouts (id · sport · name) — to find the strength
    routines (Day 1 / Day 2) to reference in the plan."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import client

    async with cli_user(email, garmin=True) as (session, user):
        rows = await run_in_threadpool(client.fetch_workouts)
    if not rows:
        print("No saved workouts found.")
        return 0
    for w in rows:
        print(f"  {w['id']}  [{w['sport'] or '—'}]  {w['name']}")
    return 0


async def _audit_calendar(email: str, delete_orphans: bool, clear_stale: bool) -> int:
    """Compare the Garmin calendar with our plan rows and name every disagreement
    (``app.garmin.calendar_audit``). Read-only by default; 0 LLM cost, Garmin reads only.

    The gap it covers: ``plan_sync`` only touches workouts by a stored id, so one we
    created but failed to record is invisible to ``unpush-plan`` and to every sync — it
    just sits on the calendar as a duplicate. ``--delete-orphans`` removes those (only
    ones whose NAME carries our push mark — Garmin exposes no author field, so this is a
    guess, which is why it is opt-in and prints every id before deleting it).
    ``--clear-stale`` is the mirror image on our side: rows pointing at a workout Garmin
    no longer has get their ids cleared, so the next sync pushes them again."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import calendar_audit, client, repository

    async with cli_user(email) as (session, user):
        rows = await repository.list_pushed_workouts(session, user.id)
        async with garmin_login(session, user):
            saved = await run_in_threadpool(client.fetch_workouts)
            items = []
            for (y, m) in calendar_audit.calendar_months():
                cal = await run_in_threadpool(client.fetch_calendar, y, m)
                if isinstance(cal, dict):
                    items.extend(cal.get("calendarItems") or [])
        found = calendar_audit.audit(rows, saved, items)

        print(f"Garmin: {len(saved)} saved workout(s), {len(items)} calendar item(s); "
              f"plan rows with a Garmin id: "
              f"{sum(1 for w in rows if w.garmin_workout_id is not None)}")

        for r in found["half_pushed"]:
            print(f"  HALF PUSHED     {r['date']} {r['type']}: workout {r['workout_id']} "
                  f"created but never scheduled (row {r['row_id']}) — next sync resumes it")
        for r in found["missing_on_garmin"]:
            print(f"  GONE ON GARMIN  {r['date']} {r['type']}: workout {r['workout_id']} "
                  f"no longer exists (row {r['row_id']})")
        for r in found["untracked_scheduled"]:
            state = "workout exists" if r["exists"] else "workout already deleted"
            print(f"  UNTRACKED SCHED {r['date']}: workout {r['workout_id']} "
                  f"{r['title']!r} — {state}")
        for r in found["untracked_workouts"]:
            print(f"  ORPHAN WORKOUT  {r['workout_id']}  {r['name']!r} — looks like ours, "
                  f"no plan row references it")

        if not any(found.values()):
            print("Nothing to report — the calendar and the plan agree.")
            return 0

        if clear_stale and found["missing_on_garmin"]:
            by_id = {w.id: w for w in rows}
            for r in found["missing_on_garmin"]:
                w = by_id[r["row_id"]]
                w.garmin_workout_id = None
                w.garmin_schedule_id = None
            await session.commit()
            print(f"Cleared stale ids on {len(found['missing_on_garmin'])} row(s) — "
                  f"the next sync will push them again.")

        if delete_orphans and found["untracked_workouts"]:
            async with garmin_login(session, user):
                for r in found["untracked_workouts"]:
                    try:
                        await run_in_threadpool(client.delete_workout, r["workout_id"])
                        print(f"  deleted orphan workout {r['workout_id']}")
                    except Exception as e:      # already gone, or a schedule Garmin won't
                        print(f"  could NOT delete {r['workout_id']}: "  # let go of
                              f"{type(e).__name__}: {str(e)[:120]}")
                    await asyncio.sleep(0.3)

        if not (delete_orphans or clear_stale):
            print("Read-only run. --clear-stale fixes our rows, --delete-orphans removes "
                  "the untracked workouts from Garmin.")
    return 0


async def _token_expiry() -> int:
    """OPS-01: read-only decode of every user's stored Garmin session — when does each
    user's login die, i.e. when a /settings re-connect becomes mandatory. Prints the
    engine that wrote each blob (``gconn`` since OPS-10, ``garth`` for a user who
    hasn't re-logged in since). Raw SQL on purpose: a diagnostic tool must work even
    on a half-migrated DB."""
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
                print(f"  {uid}  {email}: no stored Garmin session")
                continue
            try:
                info = decode_token_info(decrypt(token_enc))
            except Exception as e:
                print(f"  {uid}  {email}: undecodable token ({e})")
                continue
            print(
                f"  {uid}  {email}: [{info['kind']}] session issued"
                f" {fmt(info['session_issued'])} → dies ≈ {fmt(info['session_expiry_est'])}"
                f"  (access exp {fmt(info['access_expires_at'])},"
                f" refresh exp {fmt(info['refresh_expires_at'])})"
            )
    return 0


async def _create_user(
    email: str, password: str, is_admin: bool, seed_env: bool, backfill_month: bool
) -> int:
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

        if backfill_month:
            from app.garmin.credentials import load_credentials
            from app.garmin.providers import GarminAuthFailed
            from app.garmin.runtime import user_runtime
            from app.garmin.service import build_payload_cached

            creds = load_credentials(user)
            if not creds.has_garmin:
                print("No Garmin credentials on this user — skipping backfill "
                      "(pass --seed-env or set them up first).")
            else:
                print("Fetching last 30 days of Garmin activities/data...")
                try:
                    async with user_runtime(session, user):
                        payload, new_activities = await build_payload_cached(
                            session, user.id, days=30, activity_limit=60,
                        )
                except GarminAuthFailed:
                    print("Garmin rejected the stored email/password — backfill "
                          "skipped. Fix the creds and re-run, or push through /settings.")
                else:
                    await session.commit()
                    print(f"Backfilled {len(payload.daily)} day(s), "
                          f"{len(new_activities)} activit"
                          f"{'y' if len(new_activities) == 1 else 'ies'}.")

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
    cu.add_argument(
        "--backfill-month", action="store_true",
        help="after creation, fetch+store the last 30 days of Garmin activities/daily "
             "data (needs Garmin creds on the user, e.g. via --seed-env)",
    )

    igt = sub.add_parser("import-garth-token", help="Import a garth token dir into a user record")
    igt.add_argument("--email", required=True)
    igt.add_argument("--path", default="~/.garth", help="garth token dir (default ~/.garth)")

    bf = sub.add_parser("backfill-series", help="Fetch pace/HR series for stored runs missing one")
    bf.add_argument("--email", required=True)
    bf.add_argument("--since", help="only runs from this ISO date onward (e.g. 2025-06-01)")
    bf.add_argument("--force", action="store_true",
                    help="refetch runs that already have a series (picks up newer channels: "
                         "cadence/GCT/oscillation, coordinates) — one Garmin call per run")

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

    tpa = sub.add_parser(
        "trigger-plan-adapt",
        help="Run the weekly plan-adaptation review (EP-02) now, outside Sunday's schedule "
             "(real Claude call — sends the usual ✅/❌ proposal to Telegram if it has one)")
    tpa.add_argument("--email", required=True)

    fps = sub.add_parser(
        "fix-plan-steps",
        help="Re-cut planned sessions whose steps disagree with their dist_km — "
             "no Garmin calls, no LLM cost, read-only without --apply")
    fps.add_argument("--email", required=True)
    fps.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")

    fpk = sub.add_parser(
        "fix-plan-kinds",
        help="Clear plan sessions carrying the wrong type's columns (a run holding a "
             "strength template, a strength day holding a distance) — read-only without "
             "--apply; --repush also repairs the Garmin calendar")
    fpk.add_argument("--email", required=True)
    fpk.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    fpk.add_argument("--repush", action="store_true",
                     help="with --apply: re-push the corrected sessions to Garmin")

    lw = sub.add_parser("list-workouts", help="List the user's saved Garmin workouts (id/name)")
    lw.add_argument("--email", required=True)

    ac = sub.add_parser(
        "audit-calendar",
        help="Compare the Garmin calendar with the plan: half-pushed rows, ids Garmin no "
             "longer has, and workouts of ours nothing references (read-only by default)")
    ac.add_argument("--email", required=True)
    ac.add_argument("--delete-orphans", action="store_true",
                    help="delete the untracked workouts that look like ours from Garmin")
    ac.add_argument("--clear-stale", action="store_true",
                    help="clear stored ids that point at workouts Garmin no longer has")

    br = sub.add_parser(
        "backfill-records",
        help="Seed personal records (running + strength e1RM/tonnage) from stored history")
    br.add_argument("--email", required=True)

    brt = sub.add_parser(
        "backfill-routes",
        help="Cluster stored runs into recognised routes from their coordinates (NF-33) — "
             "no Garmin calls, no LLM cost, idempotent")
    brt.add_argument("--email", required=True)
    brt.add_argument("--since", help="only activities from this ISO date onward")

    baa = sub.add_parser(
        "backfill-activity-analysis",
        help="Restore activity analyses that were generated but never stored on the row, "
             "from report_logs — no Garmin calls, no LLM cost, read-only without --apply")
    baa.add_argument("--email", required=True)
    baa.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")

    bz = sub.add_parser(
        "backfill-zones", help="Fetch HR time-in-zone for stored activities missing it (NF-24)")
    bz.add_argument("--email", required=True)
    bz.add_argument("--days", type=int, default=180,
                    help="How far back to backfill (default 180)")

    bss = sub.add_parser(
        "backfill-strength-snapshots",
        help="Fill null strength_snapshot on the active plan's clone days (ST-09)")
    bss.add_argument("--email", required=True)

    sub.add_parser(
        "token-expiry",
        help="Decode all users' stored Garmin sessions: engine + issue/expiry dates (read-only)",
    )

    args = parser.parse_args(argv)
    # _run wraps asyncio.run to turn a _UserNotFound from cli_user into the uniform
    # "User <email> not found." + exit 1 (create-user / token-expiry never raise it).
    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Password: ")
        if not password:
            parser.error("password must not be empty")
        return _run(_create_user(
            args.email, password, args.admin, args.seed_env, args.backfill_month))
    if args.cmd == "import-garth-token":
        return _run(_import_garth_token(args.email, args.path))
    if args.cmd == "backfill-series":
        return _run(_backfill_series(args.email, args.since, args.force))
    if args.cmd == "backfill-auto-activities":
        return _run(_backfill_auto_activities(args.email, args.since))
    if args.cmd == "import-export":
        return _run(_import_export(args.email, args.path, args.overwrite, args.since))
    if args.cmd == "import-fit-series":
        return _run(_import_fit_series(args.email, args.path, args.since))
    if args.cmd == "push-plan":
        return _run(_push_plan(args.email, args.days, args.dry_run, args.date))
    if args.cmd == "unpush-plan":
        return _run(_unpush_plan(args.email, args.date))
    if args.cmd == "fix-plan-steps":
        return _run(_fix_plan_steps(args.email, args.apply))
    if args.cmd == "fix-plan-kinds":
        return _run(_fix_plan_kinds(args.email, args.apply, args.repush))
    if args.cmd == "backfill-activity-analysis":
        return _run(_backfill_activity_analysis(args.email, args.apply))
    if args.cmd == "trigger-plan-adapt":
        return _run(_trigger_plan_adapt(args.email))
    if args.cmd == "list-workouts":
        return _run(_list_workouts(args.email))
    if args.cmd == "audit-calendar":
        return _run(_audit_calendar(args.email, args.delete_orphans, args.clear_stale))
    if args.cmd == "backfill-routes":
        return _run(_backfill_routes(args.email, args.since))
    if args.cmd == "backfill-zones":
        return _run(_backfill_zones(args.email, args.days))

    if args.cmd == "backfill-records":
        return _run(_backfill_records(args.email))
    if args.cmd == "backfill-strength-snapshots":
        return _run(_backfill_strength_snapshots(args.email))
    if args.cmd == "token-expiry":
        return _run(_token_expiry())
    return 0


if __name__ == "__main__":
    sys.exit(main())
