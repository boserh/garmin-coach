"""One-off historical data-fix utilities for the active training plan.

These were once ``convert-easy-hr`` / ``fix-stride-paces`` subcommands of ``app.cli``.
They fix a *specific historical DB state* (plans generated before two ``SYSTEM_PLAN``
prompt fixes) and are not part of the living CLI, so they were moved out here
(CODE-AUDIT-2026-07 C2) to keep ``app/cli.py`` to commands still in daily use. The
logic is unchanged — the pure step-rewriting helpers stay unit-tested in
``tests/test_convert_easy_hr.py`` / ``tests/test_fix_stride_paces.py``.

Run with the venv interpreter, e.g.::

    ./venv/bin/python -m scripts.oneoff_plan_fixes convert-easy-hr --email me@example.com --dry-run
    ./venv/bin/python -m scripts.oneoff_plan_fixes fix-stride-paces --email me@example.com --dry-run
"""
import argparse
import asyncio
import datetime as dt
import re
import sys

from app.db import users
from app.db.base import async_session_maker, init_db


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

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

    args = parser.parse_args(argv)
    if args.cmd == "convert-easy-hr":
        return asyncio.run(_convert_easy_hr(
            args.email, args.easy_zone, args.recovery_zone, args.long_zone,
            args.dry_run, args.no_sync))
    if args.cmd == "fix-stride-paces":
        return asyncio.run(_fix_stride_paces(args.email, args.dry_run, args.no_sync))
    parser.error(f"unknown command {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
