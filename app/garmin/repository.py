"""Persistence: map the typed payload to ORM rows and read history back.

Keeps SQLAlchemy models and Pydantic schemas separate — the mapping between them
lives here. Upserts are idempotent on the natural key (``date`` / ``activity_id``)
and portable across SQLite and Postgres (select-then-update, no dialect-specific
ON CONFLICT).
"""
import datetime as dt
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import (
    ActivityRecord,
    BotState,
    DailyMetric,
    PersonalRecord,
    PlannedWorkout,
    ReportLog,
    TrainingPlan,
)
from app.garmin import exercises
from app.garmin.schemas import DailySummary, Payload

_DAILY_FIELDS = (
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
    "extra",
)


def _has_data(m: DailyMetric) -> bool:
    return any(getattr(m, k) is not None for k in ("sleep_score", "hrv_avg", "stress_avg"))


def _to_summary(m: DailyMetric) -> DailySummary:
    data = {k: getattr(m, k) for k in _DAILY_FIELDS}
    data["date"] = m.date
    data["has_data"] = _has_data(m)
    return DailySummary(**data)


# ---------- READ ----------

async def read_daily_metrics(
    session: AsyncSession, user_id: int, dates: List[str]
) -> Dict[str, DailySummary]:
    """Past-day metrics already stored for this user, keyed by ISO date (the
    day-level cache)."""
    if not dates:
        return {}
    rows = (
        await session.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date.in_(dates)
            )
        )
    ).scalars().all()
    return {m.date: _to_summary(m) for m in rows}


async def list_activities(session: AsyncSession, user_id: int, n: int = 5) -> List[dict]:
    """This user's most recent activities (newest first) as compact dicts for the
    bot's ``/activities`` list — keyed by the short DB ``id`` the user references."""
    rows = (
        await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user_id)
            .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
            .limit(n)
        )
    ).scalars().all()
    return [
        {"id": a.id, "date": a.date, "type": a.type, "dist_km": a.dist_km,
         "dur_min": a.dur_min, "avg_hr": a.avg_hr}
        for a in rows
    ]


ASK_MAX_ROWS = 200  # EP-09: hard cap on rows a single /ask tool call may return


async def query_activities(
    session: AsyncSession, user_id: int, *,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    type: Optional[str] = None, min_dist_km: Optional[float] = None,
    limit: int = ASK_MAX_ROWS,
) -> List[dict]:
    """EP-09 ``/ask`` tool: this user's activities in a date range (both ends inclusive,
    ISO dates), optionally filtered by activity type (substring match, e.g. "running") or a
    minimum distance. Read-only, user-scoped, newest-first, capped at ``limit`` (never above
    ``ASK_MAX_ROWS``) so a tool result stays small enough for the model to read. Compact rows
    only — no series; ``get_activity_detail`` is the drill-down for one activity."""
    stmt = select(ActivityRecord).where(ActivityRecord.user_id == user_id)
    if date_from:
        stmt = stmt.where(ActivityRecord.date >= date_from)
    if date_to:
        stmt = stmt.where(ActivityRecord.date <= date_to)
    if type:
        stmt = stmt.where(ActivityRecord.type.ilike(f"%{type}%"))
    if min_dist_km is not None:
        stmt = stmt.where(ActivityRecord.dist_km >= min_dist_km)
    stmt = stmt.order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc()) \
                .limit(max(1, min(limit, ASK_MAX_ROWS)))
    rows = (await session.execute(stmt)).scalars().all()
    out = []
    for a in rows:
        pace = round(a.dur_min / a.dist_km, 2) if (a.dur_min and a.dist_km) else None
        out.append({
            "id": a.id, "date": a.date, "type": a.type, "dist_km": a.dist_km,
            "dur_min": a.dur_min, "avg_hr": a.avg_hr, "max_hr": a.max_hr,
            "avg_pace_minkm": pace,
        })
    return out


# EP-09 /ask whitelist for query_daily/aggregate_weekly — base DailyMetric columns plus a
# subset of `extra` (everything a tool call is allowed to read; keeps a hallucinated field
# name a harmless miss instead of an arbitrary-column fishing expedition).
ASK_DAILY_BASE_FIELDS = {
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
}
ASK_DAILY_EXTRA_FIELDS = {
    "resting_hr", "readiness_score", "readiness_level", "acwr_pct", "acute_load",
    "recovery_time_h", "vo2max", "fitness_age", "endurance_score", "endurance_class",
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "spo2_avg", "respiration_avg",
}
ASK_DAILY_FIELDS = ASK_DAILY_BASE_FIELDS | ASK_DAILY_EXTRA_FIELDS


async def query_daily(
    session: AsyncSession, user_id: int, *,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    fields: Optional[List[str]] = None, limit: int = ASK_MAX_ROWS,
) -> List[dict]:
    """EP-09 ``/ask`` tool: daily recovery/sleep metrics in a date range (oldest first, so a
    trend reads left-to-right), restricted to ``ASK_DAILY_FIELDS`` — an unknown/misspelled
    field name is silently dropped rather than erroring the tool call. ``fields=None`` returns
    all whitelisted fields. Capped at ``limit`` rows (never above ``ASK_MAX_ROWS``); a day with
    no stored data yet is simply absent, not a null-filled row."""
    stmt = select(DailyMetric).where(DailyMetric.user_id == user_id)
    if date_from:
        stmt = stmt.where(DailyMetric.date >= date_from)
    if date_to:
        stmt = stmt.where(DailyMetric.date <= date_to)
    stmt = stmt.order_by(DailyMetric.date.desc()).limit(max(1, min(limit, ASK_MAX_ROWS)))
    rows = (await session.execute(stmt)).scalars().all()
    want = [f for f in (fields or sorted(ASK_DAILY_FIELDS)) if f in ASK_DAILY_FIELDS]
    if not want:
        want = sorted(ASK_DAILY_FIELDS)
    out = []
    for m in rows:
        ex = m.extra or {}
        row = {"date": m.date}
        for f in want:
            v = getattr(m, f) if f in ASK_DAILY_BASE_FIELDS else ex.get(f)
            if v is not None:
                row[f] = v
        out.append(row)
    out.reverse()  # oldest → newest
    return out


async def latest_daily_date(session: AsyncSession, user_id: int) -> Optional[str]:
    """Most recent date with a stored ``daily_metrics`` row for this user — a pure-DB,
    no-Garmin proxy for "how fresh is the data" (EP-09 ``/ask`` dedup-cache key: a coarse
    daily-slice signal, since the answer's actual DB reads only happen mid-loop)."""
    return (
        await session.execute(
            select(func.max(DailyMetric.date)).where(DailyMetric.user_id == user_id)
        )
    ).scalar_one_or_none()


async def get_activity(session: AsyncSession, user_id: int, row_id: int):
    """One activity by its DB id, scoped to the user (None if missing / not theirs)."""
    return (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.id == row_id, ActivityRecord.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def get_last_activity(session: AsyncSession, user_id: int):
    """This user's most recent activity (newest first), or None. Used by /checkin to
    target the run the runner most likely wants to rate."""
    return (
        await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user_id)
            .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def set_subjective(
    session: AsyncSession, user_id: int, row_id: int,
    *, rpe: Optional[int] = None, pain: Optional[bool] = None,
    note: Optional[str] = None,
):
    """Merge a post-run check-in (EP-12) into ``ActivityRecord.subjective``, scoped to the
    user. Only the passed fields are written, so a later tap (RPE, then a pain note) adds
    to the same record; a repeated field overwrites. Returns the row, or None if not found
    / not theirs. Does not commit."""
    act = await get_activity(session, user_id, row_id)
    if act is None:
        return None
    data = dict(act.subjective or {})
    if rpe is not None:
        data["rpe"] = rpe
    if pain is not None:
        data["pain"] = pain
        if not pain:  # "все ок" clears any earlier niggle note
            data.pop("note", None)
    if note is not None:
        data["pain"] = True
        data["note"] = note
    act.subjective = data
    # JSON column reassignment needs an explicit flag for SQLAlchemy to persist it.
    flag_modified(act, "subjective")
    return act


async def current_records(session: AsyncSession, user_id: int) -> List[PersonalRecord]:
    """The user's current best per category — the latest ``PersonalRecord`` row of each kind.
    For the ``/records`` command. Empty list when the user has no records yet."""
    rows = (
        await session.execute(
            select(PersonalRecord)
            .where(PersonalRecord.user_id == user_id)
            .order_by(PersonalRecord.id)
        )
    ).scalars().all()
    latest: Dict[str, PersonalRecord] = {}
    for r in rows:
        latest[r.kind] = r   # ordered by id asc → last (newest) wins
    return list(latest.values())


async def recent_records(
    session: AsyncSession, user_id: int, days: int = 7
) -> List[PersonalRecord]:
    """Records achieved in the last ``days`` days (newest first) — fed into the morning
    report / weekly digest so a fresh PR gets a mention (EP-14)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return list(
        (
            await session.execute(
                select(PersonalRecord)
                .where(
                    PersonalRecord.user_id == user_id,
                    PersonalRecord.date >= cutoff,
                )
                .order_by(PersonalRecord.date.desc(), PersonalRecord.id.desc())
            )
        ).scalars().all()
    )


async def get_recent_extra(session: AsyncSession, user_id: int, days: int = 21) -> dict:
    """Merge the last ``days`` days of ``extra`` into one dict — the most recent non-null
    value wins per key. Garmin metrics refresh at different cadences (race predictions &
    VO2max ~weekly, Training Readiness daily, HRV/RHR baselines slowly), so any single
    day rarely carries them all; coalescing gives plan generation the freshest value of
    each (race predictions, VO2max, endurance, ACWR/load, baselines, …)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric.extra)
            .where(
                DailyMetric.user_id == user_id,
                DailyMetric.extra.is_not(None),
                DailyMetric.date >= cutoff,
            )
            .order_by(DailyMetric.date.desc())  # newest first → first non-null wins
        )
    ).scalars().all()
    merged: dict = {}
    for ex in rows:
        if not isinstance(ex, dict):
            continue
        for k, v in ex.items():
            if v is not None and k not in merged:
                merged[k] = v
    return merged


async def read_history(session: AsyncSession, user_id: int, days: int = 30) -> List[dict]:
    """Recovery trends over the last ``days`` days for this user, oldest first."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric)
            .where(DailyMetric.user_id == user_id, DailyMetric.date >= cutoff)
            .order_by(DailyMetric.date)
        )
    ).scalars().all()
    return [
        {
            "date": m.date,
            "sleep_score": m.sleep_score,
            "sleep_h": m.sleep_h,
            "hrv_avg": m.hrv_avg,
            "hrv_status": m.hrv_status,
            "stress_avg": m.stress_avg,
            "stress_max": m.stress_max,
            "bb_charged": m.bb_charged,
            "bb_drained": m.bb_drained,
            # resting HR drift is a key fatigue marker; it lives in extra, not a column
            "resting_hr": (m.extra or {}).get("resting_hr"),
        }
        for m in rows
    ]


async def count_daily_metrics(session: AsyncSession, user_id: int) -> int:
    """Total number of stored daily rows for this user — the calibration gate for the
    injury radar (NF-04): no warnings until there's enough history to trust the signals."""
    from sqlalchemy import func

    return int(
        (await session.execute(
            select(func.count()).select_from(DailyMetric).where(
                DailyMetric.user_id == user_id
            )
        )).scalar_one()
    )


async def read_load_history(
    session: AsyncSession, user_id: int, days: int = 14
) -> List[dict]:
    """The injury radar's daily inputs (NF-04) over the last ``days`` days, oldest first:
    ``{date, hrv_avg, resting_hr, acwr_pct, hrv_baseline_low}``. ``hrv_avg`` is a column; the
    rest live in ``extra``."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric)
            .where(DailyMetric.user_id == user_id, DailyMetric.date >= cutoff)
            .order_by(DailyMetric.date)
        )
    ).scalars().all()
    out = []
    for m in rows:
        ex = m.extra or {}
        out.append({
            "date": m.date,
            "hrv_avg": m.hrv_avg,
            "resting_hr": ex.get("resting_hr"),
            "acwr_pct": ex.get("acwr_pct"),
            "hrv_baseline_low": ex.get("hrv_baseline_low"),
        })
    return out


async def recent_subjective_runs(
    session: AsyncSession, user_id: int, days: int = 14
) -> List[dict]:
    """Runs with a post-run check-in (EP-12) in the last ``days`` days, oldest first, for the
    injury radar (NF-04): ``{date, pace, rpe, pain, note}`` — ``pace`` = min/km (or None). Only
    rows carrying ``subjective`` are returned (silence isn't a signal)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(ActivityRecord)
            .where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
                ActivityRecord.subjective.is_not(None),
            )
            .order_by(ActivityRecord.date)
        )
    ).scalars().all()
    out = []
    for a in rows:
        subj = a.subjective or {}
        pace = (a.dur_min / a.dist_km) if (a.dur_min and a.dist_km) else None
        out.append({
            "date": a.date,
            "pace": pace,
            "rpe": subj.get("rpe"),
            "pain": subj.get("pain"),
            "note": subj.get("note"),
        })
    return out


async def weekly_run_volume(
    session: AsyncSession, user_id: int, weeks: int = 8
) -> List[dict]:
    """Running volume per ISO week over the last ``weeks`` weeks (oldest first), summed
    from stored activities — the core signal for calibrating safe progression (~10%/week).
    Each entry: ``{week: 'YYYY-Www', km, runs, longest_km}``."""
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    rows = (
        await session.execute(
            select(ActivityRecord.date, ActivityRecord.dist_km).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
            )
        )
    ).all()
    buckets: dict = {}
    for date_s, dist in rows:
        try:
            label = dt.date.fromisoformat(date_s).strftime("%G-W%V")
        except (TypeError, ValueError):
            continue
        b = buckets.setdefault(
            label, {"week": label, "km": 0.0, "runs": 0, "longest_km": 0.0}
        )
        km = dist or 0.0
        b["km"] += km
        b["runs"] += 1
        b["longest_km"] = max(b["longest_km"], km)
    out = sorted(buckets.values(), key=lambda x: x["week"])
    for b in out:
        b["km"] = round(b["km"], 1)
        b["longest_km"] = round(b["longest_km"], 1)
    return out


# EP-09 /ask tool: run-volume metrics reuse weekly_run_volume's buckets; anything else in
# ASK_DAILY_FIELDS is averaged per ISO week from query_daily.
_ASK_WEEKLY_RUN_KEYS = {"run_km": "km", "run_count": "runs", "longest_km": "longest_km"}
ASK_WEEKLY_METRICS = sorted(set(_ASK_WEEKLY_RUN_KEYS) | ASK_DAILY_FIELDS)


async def aggregate_weekly(
    session: AsyncSession, user_id: int, metric: str, weeks: int = 12
) -> dict:
    """EP-09 ``/ask`` tool: one metric bucketed per ISO week (oldest first) over the last
    ``weeks`` weeks (capped at 26 — a bounded tool result, not a full-history dump).
    ``metric`` is either a run-volume key (``run_km``/``run_count``/``longest_km``, from
    :func:`weekly_run_volume`) or any :data:`ASK_DAILY_FIELDS` name, averaged per week from
    :func:`query_daily`. An unknown metric returns ``{"error": ...}`` (not raised — the model
    can read the error and retry with a valid one) listing the valid names."""
    weeks = max(1, min(weeks, 26))
    if metric in _ASK_WEEKLY_RUN_KEYS:
        vol = await weekly_run_volume(session, user_id, weeks=weeks)
        key = _ASK_WEEKLY_RUN_KEYS[metric]
        return {"metric": metric, "weeks": [{"week": w["week"], "value": w[key]} for w in vol]}
    if metric in ASK_DAILY_FIELDS:
        cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
        rows = await query_daily(session, user_id, date_from=cutoff, fields=[metric])
        buckets: dict = {}
        for row in rows:
            v = row.get(metric)
            if v is None:
                continue
            try:
                label = dt.date.fromisoformat(row["date"]).strftime("%G-W%V")
            except (TypeError, ValueError):
                continue
            buckets.setdefault(label, []).append(v)
        weeks_out = [{"week": w, "value": round(sum(vs) / len(vs), 1)}
                    for w, vs in sorted(buckets.items())]
        return {"metric": metric, "weeks": weeks_out}
    return {"error": f"unknown metric '{metric}'. Valid: {ASK_WEEKLY_METRICS}"}


async def weekly_activity_load(
    session: AsyncSession, user_id: int, weeks: int = 8
) -> List[dict]:
    """Multisport training-load per ISO week over the last ``weeks`` weeks (oldest first),
    across **all** activity types (NF-05) — the cross-sport budget that the running-only
    :func:`weekly_run_volume` misses. The load math (a uniform HR/duration TRIMP proxy) lives
    in the pure ``app.multisport`` module; here we only fetch the rows."""
    from app import multisport

    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    rows = (
        await session.execute(
            select(
                ActivityRecord.date, ActivityRecord.type,
                ActivityRecord.dur_min, ActivityRecord.avg_hr,
            ).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
            )
        )
    ).all()
    acts = [{"date": d, "type": t, "dur_min": dm, "avg_hr": hr}
            for d, t, dm, hr in rows]
    return multisport.weekly_load(acts)


def _avg(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 2)


async def window_stats(
    session: AsyncSession, user_id: int, start: str, end: str
) -> dict:
    """Comparable-window aggregates (NF-06) for the inclusive ISO range ``[start, end]``:
    running volume/pace + a recovery/fitness snapshot, all from stored data. Used by
    ``run_compare`` to place "now" next to "the same span a year ago". Missing data → None/0
    fields (an empty window is a valid, honestly-empty result). ``typical_pace`` is the
    **median** run pace (robust to a stray race/interval day); race/vo2max take the best in
    the window."""
    run_rows = (
        await session.execute(
            select(
                ActivityRecord.dist_km, ActivityRecord.dur_min, ActivityRecord.avg_hr,
            ).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= start,
                ActivityRecord.date <= end,
            )
        )
    ).all()
    total_km = 0.0
    runs = 0
    longest = 0.0
    hr_vals: List[float] = []
    paces: List[float] = []
    for dist, dur, hr in run_rows:
        km = dist or 0.0
        if km <= 0:
            continue
        total_km += km
        runs += 1
        longest = max(longest, km)
        if hr:
            hr_vals.append(float(hr))
        if dur and dur > 0:
            pace = dur / km
            if 2.5 <= pace <= 12.0:   # sanity floor/ceiling (same as records.py)
                paces.append(pace)

    day_rows = (
        await session.execute(
            select(
                DailyMetric.hrv_avg, DailyMetric.sleep_score, DailyMetric.extra,
            ).where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= start,
                DailyMetric.date <= end,
            )
        )
    ).all()
    hrv_vals: List[float] = []
    sleep_vals: List[float] = []
    rhr_vals: List[float] = []
    vo2_vals: List[float] = []
    race: Dict[str, List[float]] = {}
    for hrv, sleep, ex in day_rows:
        if hrv is not None:
            hrv_vals.append(float(hrv))
        if sleep is not None:
            sleep_vals.append(float(sleep))
        if isinstance(ex, dict):
            if ex.get("vo2max"):
                vo2_vals.append(float(ex["vo2max"]))
            if ex.get("resting_hr"):
                rhr_vals.append(float(ex["resting_hr"]))
            for k in ("race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s"):
                if ex.get(k):
                    race.setdefault(k, []).append(float(ex[k]))

    return {
        "start": start,
        "end": end,
        "run_km": round(total_km, 1),
        "runs": runs,
        "longest_km": round(longest, 1),
        "typical_pace": _median(paces),
        "avg_run_hr": _avg(hr_vals),
        "avg_hrv": _avg(hrv_vals),
        "avg_sleep_score": _avg(sleep_vals),
        "avg_resting_hr": _avg(rhr_vals),
        "vo2max": max(vo2_vals) if vo2_vals else None,
        "race": {k: round(min(v)) for k, v in race.items()} if race else None,
    }


# ---------- WRITE ----------

def _dump_steps(steps) -> Optional[list]:
    """Serialize a workout's structured steps (PlanStep models or plain dicts) to a
    JSON-storable list, dropping null fields. None/empty → None (stored as JSON null)."""
    if not steps:
        return None
    out = [s.model_dump(exclude_none=True) if hasattr(s, "model_dump") else s
           for s in steps]
    return out or None


async def upsert_daily(session: AsyncSession, user_id: int, s: DailySummary) -> None:
    existing = (
        await session.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date == s.date
            )
        )
    ).scalar_one_or_none()
    fields = {k: getattr(s, k) for k in _DAILY_FIELDS}
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        session.add(DailyMetric(user_id=user_id, date=s.date, **fields))


async def upsert_activity(
    session: AsyncSession, user_id: int, activity_id: Optional[int], row: dict
) -> Optional[ActivityRecord]:
    """Upsert one activity. Returns the ORM row if it was newly inserted (so the
    caller can detect "just synced" activities), or None for an update to an
    existing row / a missing id."""
    if not activity_id:
        return None
    existing = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.activity_id == int(activity_id),
            )
        )
    ).scalar_one_or_none()
    fields = {
        "date": row.get("date") or None,
        "type": row.get("type"),
        "dur_min": row.get("dur_min"),
        "dist_km": row.get("dist_km"),
        "avg_hr": row.get("avg_hr"),
        "max_hr": row.get("max_hr"),
        "load": row.get("load"),
        "exercises": row.get("exercises"),
        "series": row.get("series"),
    }
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        return None
    rec = ActivityRecord(user_id=user_id, activity_id=int(activity_id), **fields)
    session.add(rec)
    return rec


async def persist_payload(
    session: AsyncSession, user_id: int, payload: Payload, act_pairs
) -> List[ActivityRecord]:
    """Upsert everything the payload carries for this user (does not commit).
    Returns newly inserted activity rows (never updates) — the caller uses this to
    trigger auto-analysis of freshly synced activities."""
    for d in payload.daily:
        if d.has_data:
            await upsert_daily(session, user_id, d)
    new_activities: List[ActivityRecord] = []
    for activity_id, row in act_pairs:
        rec = await upsert_activity(session, user_id, activity_id, row)
        if rec is not None:
            new_activities.append(rec)
    if new_activities:
        await session.flush()  # assign ids before the caller reads them
    return new_activities


async def get_last_report(session: AsyncSession, user_id: int):
    """This user's most recent *daily* report from a prior day, as (text, date_iso),
    for day-over-day continuity context.

    Excludes today's reports (so repeated same-day /report presses keep a stable
    dedup-cache key instead of each picking up the previous press as "previous"),
    and excludes /deep and /ask — only the daily-status thread (report/morning)."""
    today_start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    row = (
        await session.execute(
            select(ReportLog.report_text, ReportLog.created_at)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.report_text.is_not(None),
                ReportLog.ok.is_(True),
                ReportLog.kind.in_(("report", "morning")),
                ReportLog.created_at < today_start,
            )
            .order_by(ReportLog.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    text, created = row
    return text, (created.date().isoformat() if created else None)


async def get_recent_reports(
    session: AsyncSession, user_id: int, n: int = 3
) -> List[dict]:
    """This user's last ``n`` delivered daily reports (newest first) as
    [{date, text}, ...], for the /ask follow-up context. Only kind="report" — /deep
    and /ask answers are excluded so the context stays the daily-report thread."""
    rows = (
        await session.execute(
            select(ReportLog.report_text, ReportLog.created_at)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.report_text.is_not(None),
                ReportLog.ok.is_(True),
                ReportLog.kind == "report",
            )
            .order_by(ReportLog.created_at.desc())
            .limit(n)
        )
    ).all()
    return [
        {"date": created.date().isoformat() if created else None, "text": text}
        for text, created in rows
    ]


async def get_recent_asks(
    session: AsyncSession, user_id: int, minutes: int = 5
) -> List[dict]:
    """This user's successful /ask exchanges from the last ``minutes`` minutes, oldest
    first, as [{question, answer}, ...] — the short conversation thread so a follow-up
    /ask can build on what was just asked."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    rows = (
        await session.execute(
            select(ReportLog.question, ReportLog.report_text)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.kind == "ask",
                ReportLog.ok.is_(True),
                ReportLog.report_text.is_not(None),
                ReportLog.created_at >= cutoff,
            )
            .order_by(ReportLog.created_at.asc())
        )
    ).all()
    return [{"question": q, "answer": a} for q, a in rows]


async def log_report(
    session: AsyncSession,
    *,
    user_id: Optional[int] = None,
    kind: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    ok: bool = True,
    cached: bool = False,
    error: Optional[str] = None,
    question: Optional[str] = None,
    report_text: Optional[str] = None,
    tool_rounds: Optional[int] = None,
) -> None:
    session.add(ReportLog(
        user_id=user_id, kind=kind, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cost_usd=cost_usd, ok=ok, cached=cached,
        error=error, question=question, report_text=report_text,
        tool_rounds=tool_rounds,
    ))
    await session.commit()


# ---------- BOT STATE ----------

async def get_state(session: AsyncSession, user_id: int, key: str) -> Optional[str]:
    m = await session.get(BotState, (user_id, key))
    return m.value if m else None


async def set_state(session: AsyncSession, user_id: int, key: str, value: str) -> None:
    m = await session.get(BotState, (user_id, key))
    if m:
        m.value = value
    else:
        session.add(BotState(user_id=user_id, key=key, value=value))
    await session.commit()


# ---------- TRAINING PLAN ----------

async def get_active_plan(session: AsyncSession, user_id: int):
    """This user's current active TrainingPlan, or None."""
    return (
        await session.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
            .order_by(TrainingPlan.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_plans(session: AsyncSession, user_id: int, status: Optional[str] = None):
    """This user's plans (newest first); optionally filtered by status."""
    stmt = select(TrainingPlan).where(TrainingPlan.user_id == user_id)
    if status:
        stmt = stmt.where(TrainingPlan.status == status)
    return (await session.execute(stmt.order_by(TrainingPlan.id.desc()))).scalars().all()


async def get_plan(session: AsyncSession, user_id: int, plan_id: int):
    """One plan by id, scoped to the user (active or archived)."""
    return (
        await session.execute(
            select(TrainingPlan).where(
                TrainingPlan.id == plan_id, TrainingPlan.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def list_workouts(
    session: AsyncSession, plan_id: int, *, upcoming_only: bool = False
) -> List[PlannedWorkout]:
    """Workouts of a plan, oldest first. ``upcoming_only`` keeps today+ planned ones."""
    stmt = select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
    if upcoming_only:
        stmt = stmt.where(
            PlannedWorkout.date >= dt.date.today().isoformat(),
            PlannedWorkout.status == "planned",
        )
    return (await session.execute(stmt.order_by(PlannedWorkout.date))).scalars().all()


async def get_workout_for_activity(
    session: AsyncSession, user_id: int, activity_id: int
) -> Optional[PlannedWorkout]:
    """The PlannedWorkout (if any) matched to this activity by ``matching.match_activities``
    (``completed_activity_id``). Scoped to the user so cross-user ids can't leak."""
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.user_id == user_id,
                PlannedWorkout.completed_activity_id == activity_id,
            )
        )
    ).scalar_one_or_none()


async def upcoming_plan_workouts(
    session: AsyncSession, user_id: int, days: int = 2
) -> List[PlannedWorkout]:
    """Today's and the next ``days-1`` days' planned workouts from the active plan.
    Returns [] when there is no active plan or nothing in the window."""
    plan = await get_active_plan(session, user_id)
    if plan is None:
        return []
    today = dt.date.today()
    window_end = (today + dt.timedelta(days=days - 1)).isoformat()
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.plan_id == plan.id,
                PlannedWorkout.date >= today.isoformat(),
                PlannedWorkout.date <= window_end,
                PlannedWorkout.status == "planned",
            ).order_by(PlannedWorkout.date)
        )
    ).scalars().all()


async def weekly_compliance(
    session: AsyncSession, plan_id: int
) -> dict:
    """Per-week compliance summary for a plan, keyed by ISO week string ('YYYY-Www').

    Each entry: ``{total, done, pace_deltas: [float, ...], overreached}``.
    * ``total`` — run-type workouts (not rest/cross/strength) in that week.
    * ``done`` — workouts with status done or partial.
    * ``pace_deltas`` — list of (actual − plan) pace values in min/km for matched workouts
      where both sides are known (positive = slower, negative = faster).
    * ``overreached`` — count of *easy-intent* sessions (easy/recovery/base/long) done but
      whose post-run check-in RPE was hard (≥ ``subjective.HARD_RPE``): "did it, but it felt
      much harder than the session called for" (EP-12 phase 3 plan/fact status). Zero when
      there are no check-ins.
    """
    from app import subjective as subjective_mod

    workouts = (
        await session.execute(
            select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
        )
    ).scalars().all()

    # RPE per matched activity, for the overreached flag (one query for all done workouts).
    done_ids = [w.completed_activity_id for w in workouts if w.completed_activity_id]
    rpe_by_id: dict = {}
    if done_ids:
        arows = (
            await session.execute(
                select(ActivityRecord.id, ActivityRecord.subjective).where(
                    ActivityRecord.id.in_(done_ids)
                )
            )
        ).all()
        for aid, subj in arows:
            if isinstance(subj, dict) and isinstance(subj.get("rpe"), (int, float)):
                rpe_by_id[aid] = subj["rpe"]

    _SKIP = {"rest", "cross", "strength"}
    buckets: dict = {}
    for w in workouts:
        if (w.type or "").lower() in _SKIP:
            continue
        try:
            week = dt.date.fromisoformat(w.date).strftime("%G-W%V")
        except (ValueError, TypeError):
            continue
        b = buckets.setdefault(
            week, {"total": 0, "done": 0, "pace_deltas": [], "overreached": 0})
        b["total"] += 1
        if w.status in ("done", "partial"):
            b["done"] += 1
            if isinstance(w.match_info, dict):
                ap = w.match_info.get("actual_pace_minkm")
                pp = w.match_info.get("plan_pace_minkm")
                if ap is not None and pp is not None:
                    b["pace_deltas"].append(round(ap - pp, 2))
            rpe = rpe_by_id.get(w.completed_activity_id)
            if (rpe is not None and rpe >= subjective_mod.HARD_RPE
                    and (w.type or "").lower() in subjective_mod.EASY_TYPES):
                b["overreached"] += 1
    return buckets


async def list_pushed_workouts(session: AsyncSession, user_id: int) -> List[PlannedWorkout]:
    """This user's workouts already pushed to Garmin (``garmin_workout_id`` set), across
    all plans — for the sync cleanup pass. (A BigInteger column → real SQL NULL, so
    ``is_not(None)`` works here, unlike the JSON ``series`` gotcha.)"""
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.user_id == user_id,
                PlannedWorkout.garmin_workout_id.is_not(None),
            )
        )
    ).scalars().all()


async def create_plan(
    session: AsyncSession,
    user_id: int,
    *,
    goal: str,
    goal_label: Optional[str],
    target_date: Optional[str],
    start_date: Optional[str],
    days_per_week: Optional[int],
    intensity: Optional[str],
    intake: Optional[dict],
    summary: Optional[str],
    workouts: list,
) -> TrainingPlan:
    """Create a new active plan (archiving any prior active one) and its workouts.
    ``workouts`` is a list of ``PlanWorkout`` (or anything with the same attrs)."""
    prior = (
        await session.execute(
            select(TrainingPlan).where(
                TrainingPlan.user_id == user_id, TrainingPlan.status == "active"
            )
        )
    ).scalars().all()
    for p in prior:
        p.status = "archived"

    plan = TrainingPlan(
        user_id=user_id, goal=goal, goal_label=goal_label, target_date=target_date,
        start_date=start_date, days_per_week=days_per_week, intensity=intensity,
        intake=intake, summary=summary, status="active",
    )
    session.add(plan)
    await session.flush()  # assign plan.id
    for w in workouts:
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=user_id, date=w.date, week=w.week,
            type=w.type, dist_km=w.dist_km, description=w.description,
            steps=_dump_steps(getattr(w, "steps", None)), status="planned",
        ))
    await session.commit()
    return plan


async def archive_plan(session: AsyncSession, plan: TrainingPlan) -> None:
    plan.status = "archived"
    await session.commit()


async def last_workout_date(session: AsyncSession, plan_id: int) -> Optional[str]:
    """The latest workout date (ISO string) in a plan, or None if it has no workouts.
    Used by the open-ended auto-extend job to know how far the plan currently reaches."""
    return (
        await session.execute(
            select(func.max(PlannedWorkout.date)).where(
                PlannedWorkout.plan_id == plan_id
            )
        )
    ).scalar_one_or_none()


async def append_workouts(
    session: AsyncSession, plan: TrainingPlan, workouts: list, *, week_offset: int = 0
) -> int:
    """Append more run workouts to an EXISTING plan (open-ended extension) — unlike
    ``create_plan`` this neither archives the plan nor touches prior rows. ``week_offset``
    is added to each workout's ``week`` so the new block continues the plan's numbering.
    Returns the number of rows added."""
    added = 0
    for w in workouts:
        base_week = getattr(w, "week", None) or 1
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=plan.user_id, date=w.date,
            week=base_week + week_offset,
            type=w.type, dist_km=w.dist_km, description=w.description,
            steps=_dump_steps(getattr(w, "steps", None)), status="planned",
        ))
        added += 1
    await session.commit()
    return added


_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def add_strength_workouts(session: AsyncSession, plan: TrainingPlan,
                                assignments: dict, snapshots: Optional[dict] = None,
                                custom: Optional[dict] = None, *,
                                start: Optional[str] = None, end: Optional[str] = None,
                                week_offset: int = 0) -> int:
    """Add strength sessions on fixed weekdays across the plan's date range. ``assignments``
    maps a weekday slug (mon..sun) → {"id", "name"} of the saved Garmin workout to place on
    that weekday **every week** (a fixed pairing, not a rotation). Each carries a
    ``garmin_template_id`` (cloned on push). ``snapshots`` (optional, keyed by workout id)
    caches each template's contents ({name?, exercises}) onto the row's ``strength_snapshot``
    so ``/plan`` renders the exercise accordion from the DB. ``custom`` maps a weekday slug →
    an already-sanitised ``strength_plan`` dict (the setup form's free-text "інше…" sessions,
    built natively on push). A weekday in both prefers the saved workout. ``start``/``end``
    (ISO) override the plan's date range — the open-ended extension passes the new block's
    window so strength lands only on the freshly-added weeks; ``week_offset`` continues the
    plan's week numbering. Returns the count."""
    snapshots = snapshots or {}
    by_wd = {}
    for slug, t in (assignments or {}).items():
        wd = _WEEKDAY.get(slug)
        if wd is not None and t and t.get("id"):
            by_wd[wd] = t
    custom_by_wd = {}
    for slug, sp in (custom or {}).items():
        wd = _WEEKDAY.get(slug)
        if wd is not None and sp:
            custom_by_wd[wd] = sp
    if not by_wd and not custom_by_wd:
        return 0
    # ``start``/``end`` override the plan's own range — used by the open-ended extension to
    # lay strength only across the freshly-added block. Default to the plan's date range.
    try:
        start_d = dt.date.fromisoformat(start or plan.start_date)
    except (ValueError, TypeError):
        return 0
    try:
        end_d = dt.date.fromisoformat(end or plan.target_date)
    except (ValueError, TypeError):
        end_d = start_d + dt.timedelta(weeks=12)
    if end_d < start_d:
        end_d = start_d + dt.timedelta(weeks=12)
    added = 0
    d = start_d
    while d <= end_d:
        wd = d.weekday()
        week = (d - start_d).days // 7 + 1 + week_offset
        t = by_wd.get(wd)
        cp = custom_by_wd.get(wd)
        if t is not None:
            session.add(PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=d.isoformat(),
                week=week, type="strength",
                description=t.get("name") or "Силова",
                garmin_template_id=t.get("id"),
                strength_snapshot=snapshots.get(t.get("id")), status="planned"))
            added += 1
        elif cp is not None:
            session.add(PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=d.isoformat(),
                week=week, type="strength",
                description=cp.get("name") or "Силова",
                strength_plan=cp, status="planned"))
            added += 1
        d += dt.timedelta(days=1)
    await session.commit()
    return added


async def _workout_on(session: AsyncSession, plan_id: int, date: str):
    return (
        await session.execute(
            select(PlannedWorkout)
            .where(PlannedWorkout.plan_id == plan_id, PlannedWorkout.date == date)
            .order_by(PlannedWorkout.id)
            .limit(1)
        )
    ).scalar_one_or_none()


def _sanitize_strength(sp) -> Optional[dict]:
    """Validate a ``StrengthSession``(-like) into the stored ``strength_plan`` dict: keep
    only exercises whose ``category`` is a real Garmin code (so a hallucinated code never
    reaches the watch), drop empty blocks. Returns None if nothing valid remains."""
    if sp is None:
        return None
    data = sp.model_dump() if hasattr(sp, "model_dump") else dict(sp)
    blocks_out = []
    for b in data.get("blocks") or []:
        exs = []
        for e in b.get("exercises") or []:
            cat = (e.get("category") or "").upper()
            if not exercises.valid_category(cat):
                continue
            ex = exercises.check_exercise(cat, e.get("exercise"))
            exs.append({"category": cat, "exercise": ex,
                        "reps": e.get("reps"), "weight_kg": e.get("weight_kg")})
        if exs:
            blocks_out.append({"reps": int(b.get("reps") or 1),
                               "rest_s": b.get("rest_s"), "exercises": exs})
    if not blocks_out:
        return None
    return {"name": data.get("name"), "warmup_s": data.get("warmup_s"),
            "blocks": blocks_out}


async def apply_plan_ops(
    session: AsyncSession, plan: TrainingPlan, ops: list
) -> List[PlannedWorkout]:
    """Apply edit operations (``PlanOp``-like objects) to a plan's workouts. Returns the
    **touched** workouts (so the caller can re-sync just those to Garmin). ``move``/
    ``modify``/``skip`` target the workout on ``op.date``."""
    affected: List[PlannedWorkout] = []
    for op in ops:
        if op.action == "add":
            w = PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=op.date, week=op.week,
                type=op.type or "easy", dist_km=op.dist_km,
                description=op.description or "",
                steps=_dump_steps(getattr(op, "steps", None)),
                garmin_template_id=getattr(op, "garmin_template_id", None),
                strength_plan=_sanitize_strength(getattr(op, "strength", None)),
                status="planned",
            )
            session.add(w)
            affected.append(w)
            continue
        w = await _workout_on(session, plan.id, op.date)
        if w is None:
            continue
        if op.action == "skip":
            w.status = "skipped"
            affected.append(w)
        elif op.action == "move" and op.to_date:
            w.date = op.to_date
            affected.append(w)
        elif op.action == "modify":
            if op.type is not None:
                w.type = op.type
            if op.dist_km is not None:
                w.dist_km = op.dist_km
            if op.description is not None:
                w.description = op.description
            if getattr(op, "steps", None) is not None:
                w.steps = _dump_steps(op.steps)
            if getattr(op, "garmin_template_id", None) is not None:
                w.garmin_template_id = op.garmin_template_id
            if getattr(op, "strength", None) is not None:
                sp = _sanitize_strength(op.strength)
                if sp:
                    w.strength_plan = sp
            affected.append(w)
        elif op.action == "swap_exercise":
            frm = (getattr(op, "from_category", None) or "").upper()
            to = (getattr(op, "to_category", None) or "").upper()
            # reject an unmapped/invalid target so a hallucinated code never reaches Garmin
            if not frm or not exercises.valid_category(to):
                continue
            # validate the exercise name against the *target* category (it belongs to `to`)
            edit = {
                "from": frm, "to": to,
                "exercise": exercises.check_exercise(to, getattr(op, "exercise", None)),
                "reps": getattr(op, "reps", None),
            }
            w.exercise_edits = list(w.exercise_edits or []) + [edit]
            affected.append(w)
    await session.commit()
    return affected
