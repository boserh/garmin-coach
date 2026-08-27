"""Find what the Garmin calendar holds that our plan rows don't account for.

Why this exists. ``plan_sync`` only ever touches workouts by a **stored id**, which makes
it safe (it can't delete the athlete's own Runna/manual sessions) and blind in exactly one
direction: a workout we created whose id never reached the database is invisible to every
cleanup path we have. It also can't be seen through ``service.fetch_planned``, because
``/calendar-service`` hides a schedule whose workout has since been deleted — the live case
on 2026-08-27 showed one calendar item where ``/workout-service/schedule/summaries``
(what the Connect web UI reads) showed two.

So the audit compares three lists — our rows, Garmin's saved workouts, Garmin's calendar —
and names the four ways they can disagree. Pure: the caller fetches, this classifies, the
tests need no mocks.

Ownership is a **guess by name**, and deliberately a narrow one: only a name carrying one of
``workout_export.NAME_MARKS`` is treated as ours. Garmin stores no "created by" field a
third-party token can read, so a workout the athlete named with the same emoji would be
misread — hence nothing here deletes anything on its own.
"""
import datetime as dt
from typing import Iterable, Optional

from app.garmin import workout_export


def looks_ours(name: Optional[str]) -> bool:
    """True when a Garmin workout name carries one of our push marks. Matches anywhere in
    the first characters rather than a strict prefix — Garmin has been seen returning the
    calendar title with the mark followed by a doubled space."""
    return bool(name) and any(name.lstrip().startswith(m) for m in workout_export.NAME_MARKS)


def audit(rows: Iterable, garmin_workouts: Iterable[dict],
          calendar_items: Iterable[dict]) -> dict:
    """Classify the disagreements. Returns four lists, each of plain dicts:

    * ``half_pushed`` — our row has a workout id but no schedule id: the push was
      interrupted between Garmin's two calls. The next sync resumes it; listed so a run
      that never resumes is visible.
    * ``missing_on_garmin`` — our row points at a workout id Garmin no longer has (deleted
      by hand, or by a failed sync). The row claims to be on the watch and isn't.
    * ``untracked_workouts`` — saved workouts on Garmin that look like ours and that no row
      references: the orphans this module exists for.
    * ``untracked_scheduled`` — calendar entries pointing at a workout id we don't track.
      A subset of the above once scheduled, plus anything scheduled whose workout is gone.

    ``rows`` are ``PlannedWorkout``s; ``garmin_workouts`` come from
    ``client.fetch_workouts`` (``id``/``name``), ``calendar_items`` from
    ``client.fetch_calendar`` (``date``/``workoutId``/``title``)."""
    rows = list(rows)
    tracked = {w.garmin_workout_id for w in rows if w.garmin_workout_id is not None}

    half_pushed = [
        {"row_id": w.id, "date": w.date, "type": w.type, "workout_id": w.garmin_workout_id}
        for w in rows
        if w.garmin_workout_id is not None and w.garmin_schedule_id is None
    ]

    on_garmin = {gw["id"]: gw.get("name") for gw in garmin_workouts if gw.get("id")}
    missing_on_garmin = [
        {"row_id": w.id, "date": w.date, "type": w.type, "workout_id": w.garmin_workout_id}
        for w in rows
        if w.garmin_workout_id is not None and w.garmin_workout_id not in on_garmin
    ]

    untracked_workouts = [
        {"workout_id": wid, "name": name}
        for wid, name in on_garmin.items()
        if wid not in tracked and looks_ours(name)
    ]

    untracked_scheduled = []
    for item in calendar_items:
        if item.get("itemType") != "workout":
            continue
        wid = item.get("workoutId")
        if wid is None or wid in tracked:
            continue
        title = item.get("title")
        if not looks_ours(title):
            continue      # the athlete's own Runna/manual session — not ours to report
        untracked_scheduled.append(
            {"workout_id": wid, "date": item.get("date"), "title": title,
             "exists": wid in on_garmin})

    return {"half_pushed": half_pushed, "missing_on_garmin": missing_on_garmin,
            "untracked_workouts": untracked_workouts,
            "untracked_scheduled": sorted(untracked_scheduled,
                                          key=lambda x: (x["date"] or "", x["workout_id"]))}


def calendar_months(today: Optional[dt.date] = None) -> list:
    """The ``(year, month_index)`` pairs to read for an audit: this month and the next, the
    same span ``service.fetch_planned`` covers (Garmin's month index is 0-based)."""
    today = today or dt.date.today()
    nxt_y = today.year + (today.month // 12)
    nxt_m = (today.month % 12) + 1
    return [(today.year, today.month - 1), (nxt_y, nxt_m - 1)]
