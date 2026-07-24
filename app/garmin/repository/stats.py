"""Window / weekly training-statistics reads (NF-05/06/07): weekly volume,
cross-sport load, compare-window and Wrapped aggregates. Split out of the flat
``repository.py`` (B1). Depends on ``core.query_daily`` / ``ASK_DAILY_FIELDS``."""
import datetime as dt
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityRecord,
    DailyMetric,
    PersonalRecord,
)
from app.garmin.repository.core import ASK_DAILY_FIELDS, query_daily
from app.statutil import avg as _avg
from app.statutil import median as _median


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


async def runs_for_efficiency(
    session: AsyncSession, user_id: int, weeks: int = 12
) -> List[dict]:
    """NF-19: runs over the last ``weeks`` weeks that carry a per-point ``series`` AND an
    average HR — the raw material for the aerobic-efficiency trend (pace@HR). Filtered to
    running type; the easy-vs-hard split and the ≥30-min gate live in ``app.efficiency``
    (kept out of SQL so it's testable). The JSON ``series`` null is filtered in Python (a
    JSON column stores ``None`` as JSON ``null``, not SQL NULL — the README gotcha)."""
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    rows = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
                ActivityRecord.avg_hr.is_not(None),
            ).order_by(ActivityRecord.date)
        )
    ).scalars().all()
    return [
        {"date": a.date, "type": a.type, "dur_min": a.dur_min, "dist_km": a.dist_km,
         "avg_hr": a.avg_hr, "series": a.series}
        for a in rows if a.series
    ]


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


async def records_in_range(
    session: AsyncSession, user_id: int, start: str, end: str
) -> List[PersonalRecord]:
    """PersonalRecords achieved within the inclusive ISO date range ``[start, end]`` (newest
    first) — the milestones for a Wrapped review (NF-07)."""
    return list(
        (
            await session.execute(
                select(PersonalRecord)
                .where(
                    PersonalRecord.user_id == user_id,
                    PersonalRecord.date >= start,
                    PersonalRecord.date <= end,
                )
                .order_by(PersonalRecord.date.desc(), PersonalRecord.id.desc())
            )
        ).scalars().all()
    )


async def wrapped_stats(
    session: AsyncSession, user_id: int, start: str, end: str
) -> dict:
    """Period aggregate for a Wrapped review (NF-07): the run/recovery numbers from
    :func:`window_stats` (reused — no duplicate volume math), augmented with the whole-period
    extras a recap wants: an all-sport activity breakdown + hours, the biggest running week
    in the window, and the VO2max arc (first vs last). All from stored data; an empty window
    is a valid, honestly-empty result."""
    from app import multisport

    base = await window_stats(session, user_id, start, end)

    # all-sport breakdown + total moving time
    act_rows = (
        await session.execute(
            select(
                ActivityRecord.type, ActivityRecord.dur_min,
            ).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= start,
                ActivityRecord.date <= end,
            )
        )
    ).all()
    sports: Dict[str, int] = {}
    total_min = 0.0
    for a_type, dur in act_rows:
        sports[multisport.sport_bucket(a_type)] = sports.get(multisport.sport_bucket(a_type), 0) + 1
        total_min += dur or 0.0

    # biggest running week within the window (by km)
    week_km: Dict[str, float] = {}
    run_rows = (
        await session.execute(
            select(ActivityRecord.date, ActivityRecord.dist_km).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= start,
                ActivityRecord.date <= end,
            )
        )
    ).all()
    for date_s, dist in run_rows:
        try:
            wk = dt.date.fromisoformat(date_s).strftime("%G-W%V")
        except (TypeError, ValueError):
            continue
        week_km[wk] = week_km.get(wk, 0.0) + (dist or 0.0)
    biggest_week = None
    if week_km:
        wk, km = max(week_km.items(), key=lambda kv: kv[1])
        biggest_week = {"week": wk, "km": round(km, 1)}

    # VO2max arc — first vs last non-null in the window
    vo2_rows = (
        await session.execute(
            select(DailyMetric.date, DailyMetric.extra)
            .where(
                DailyMetric.user_id == user_id,
                DailyMetric.date >= start,
                DailyMetric.date <= end,
                DailyMetric.extra.is_not(None),
            )
            .order_by(DailyMetric.date)
        )
    ).all()
    vo2_series = [float(ex["vo2max"]) for _, ex in vo2_rows
                  if isinstance(ex, dict) and ex.get("vo2max")]

    base.update({
        "sports": {k: v for k, v in sorted(sports.items(), key=lambda kv: -kv[1])},
        "total_activities": len(act_rows),
        "total_hours": round(total_min / 60, 1) if total_min else 0.0,
        "biggest_week": biggest_week,
        "vo2_start": vo2_series[0] if vo2_series else None,
        "vo2_end": vo2_series[-1] if vo2_series else None,
    })
    return base
