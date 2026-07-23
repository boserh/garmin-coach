"""Personal records & milestones (EP-14) — a pure-Python, zero-LLM detector.

All the raw material already lives in the DB: ``activities`` (distance/duration/pace over
years), and ``daily_metrics.extra`` (VO2max, race predictions). :func:`detect_records`
recomputes the current best per category, inserts a ``PersonalRecord`` row whenever one is
beaten (carrying the ``previous_value`` it dethroned), and returns the freshly-inserted rows.

The **backfill-vs-fresh** distinction (AC: no celebrations during import) is a date gate,
not a flag: every record carries the real date it was achieved, so a first run over years of
history dates its bests in the past and :func:`announce_worthy` filters them all out — only a
record achieved in the last few days is worth a "🎉". No LLM, no network; cheap enough to run
on every morning tick.
"""
import datetime as dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import format as fmt
from app.db.models import ActivityRecord, DailyMetric, PersonalRecord

# Only records achieved within this many days of "now" are announced — everything older is
# treated as backfill (silently seeded into the table). See the module docstring.
FRESH_DAYS = 3

# Sanity floors for pace records: reject a sub-2:30/km "run" (GPS glitch / mislabelled data)
# and anything slower than 12:00/km (a walk, not a run PR).
MIN_PACE_MIN_KM = 2.5
MAX_PACE_MIN_KM = 12.0

# Race predictions jitter daily; only count an improvement of at least this many seconds as a
# new record so we don't ping on noise (EP-14 pitfall).
RACE_IMPROVE_S = 10.0

# Distance windows (±5%) that qualify an activity as "a 5K / 10K / half".
_HALF_KM = 21.0975
_DIST_WINDOWS = {
    "fastest_5k": (5.0 * 0.95, 5.0 * 1.05),
    "fastest_10k": (10.0 * 0.95, 10.0 * 1.05),
    "fastest_half": (_HALF_KM * 0.95, _HALF_KM * 1.05),
}
_PACE_KINDS = frozenset(_DIST_WINDOWS)

_RACE_KEYS = {
    "race_5k": "race_5k_s",
    "race_10k": "race_10k_s",
    "race_half": "race_half_s",
    "race_marathon": "race_marathon_s",
}
_RACE_KINDS = frozenset(_RACE_KEYS)

# Higher value is better for these; for everything else (pace + race predictions) lower wins.
_HIGHER_BETTER = frozenset(
    {"longest_run_km", "longest_run_min", "biggest_week_km", "vo2max"}
)

LABELS = {
    "fastest_5k": "найшвидші 5 км",
    "fastest_10k": "найшвидші 10 км",
    "fastest_half": "найшвидший півмарафон",
    "longest_run_km": "найдовша пробіжка",
    "longest_run_min": "найтриваліша пробіжка",
    "biggest_week_km": "найоб'ємніший тиждень",
    "vo2max": "VO2max",
    "race_5k": "прогноз на 5 км",
    "race_10k": "прогноз на 10 км",
    "race_half": "прогноз на півмарафон",
    "race_marathon": "прогноз на марафон",
}

# Ordering for the /records list (best-to-worst reading order).
DISPLAY_ORDER = (
    "fastest_5k", "fastest_10k", "fastest_half",
    "longest_run_km", "longest_run_min", "biggest_week_km",
    "vo2max", "race_5k", "race_10k", "race_half", "race_marathon",
)

# Rounding precision per kind — applied to the candidate before it's compared and stored, so
# a re-detection of the same best isn't a hair-off "improvement" that re-inserts forever.
_ROUND = {
    "longest_run_min": 0, "biggest_week_km": 1, "longest_run_km": 1, "vo2max": 0,
}


def _higher_better(kind: str) -> bool:
    return kind in _HIGHER_BETTER


def _round_for(kind: str, value: float) -> float:
    if kind in _PACE_KINDS:
        return round(value, 2)   # ~0.6 s/km granularity
    if kind in _RACE_KINDS:
        return round(value)      # whole seconds
    return round(value, _ROUND.get(kind, 1))


@dataclass
class _Candidate:
    value: float
    date: str
    activity_id: Optional[int] = None


def _offer(out: dict, kind: str, value: float, date: str, activity_id: Optional[int]) -> None:
    """Keep the better of the current and the offered candidate for ``kind`` (ties go to the
    earlier date — the record was first set then)."""
    value = _round_for(kind, value)
    cur = out.get(kind)
    if cur is None:
        out[kind] = _Candidate(value, date, activity_id)
        return
    better = value > cur.value if _higher_better(kind) else value < cur.value
    if better or (value == cur.value and date < cur.date):
        out[kind] = _Candidate(value, date, activity_id)


async def _run_bests(session: AsyncSession, user_id: int) -> dict:
    """Best run-derived candidates (pace / longest / biggest week) over all stored runs."""
    rows = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
            )
        )
    ).scalars().all()

    out: dict = {}
    week_km: dict = {}
    week_date: dict = {}
    for a in rows:
        km = a.dist_km or 0.0
        mins = a.dur_min or 0.0
        if km > 0:
            _offer(out, "longest_run_km", km, a.date, a.id)
            try:
                wk = dt.date.fromisoformat(a.date).strftime("%G-W%V")
            except (TypeError, ValueError):
                wk = None
            if wk:
                week_km[wk] = week_km.get(wk, 0.0) + km
                # Last run date in the week — recent for the current week, so its record is
                # announceable; old for a historical big week (stays a silent backfill).
                if a.date > week_date.get(wk, ""):
                    week_date[wk] = a.date
        if mins > 0:
            _offer(out, "longest_run_min", mins, a.date, a.id)
        if km > 0 and mins > 0:
            pace = mins / km
            if MIN_PACE_MIN_KM <= pace <= MAX_PACE_MIN_KM:
                for kind, (lo, hi) in _DIST_WINDOWS.items():
                    if lo <= km <= hi:
                        _offer(out, kind, pace, a.date, a.id)

    for wk, km in week_km.items():
        _offer(out, "biggest_week_km", km, week_date[wk], None)
    return out


async def _metric_bests(session: AsyncSession, user_id: int) -> dict:
    """Best all-time VO2max and race predictions from ``daily_metrics.extra``."""
    rows = (
        await session.execute(
            select(DailyMetric.date, DailyMetric.extra).where(
                DailyMetric.user_id == user_id,
                DailyMetric.extra.is_not(None),
                DailyMetric.date.is_not(None),
            )
        )
    ).all()

    out: dict = {}
    for date_s, ex in rows:
        if not isinstance(ex, dict):
            continue
        vo2 = ex.get("vo2max")
        if vo2:
            _offer(out, "vo2max", float(vo2), date_s, None)
        for kind, key in _RACE_KEYS.items():
            rv = ex.get(key)
            if rv:
                _offer(out, kind, float(rv), date_s, None)
    return out


def _beats(kind: str, value: float, prev: Optional[float]) -> bool:
    if prev is None:
        return True
    if _higher_better(kind):
        return value > prev
    if kind in _RACE_KINDS:            # noisy — require a meaningful margin
        return value <= prev - RACE_IMPROVE_S
    return value < prev                # pace records: any real improvement counts


async def _latest_values(session: AsyncSession, user_id: int) -> dict:
    """Current stored best per kind (the latest ``PersonalRecord`` row of each kind)."""
    rows = (
        await session.execute(
            select(PersonalRecord)
            .where(PersonalRecord.user_id == user_id)
            .order_by(PersonalRecord.id)
        )
    ).scalars().all()
    latest: dict = {}
    for r in rows:
        latest[r.kind] = r.value   # ordered by id asc → last wins
    return latest


async def detect_records(session: AsyncSession, user_id: int) -> list:
    """Recompute every category and insert a ``PersonalRecord`` for each newly-beaten best.
    Returns the inserted rows (not committed — the caller owns the transaction). Idempotent:
    an unchanged best won't re-insert. Use :func:`announce_worthy` to pick the fresh ones."""
    bests = await _run_bests(session, user_id)
    bests.update(await _metric_bests(session, user_id))
    stored = await _latest_values(session, user_id)

    inserted = []
    for kind, cand in bests.items():
        prev = stored.get(kind)
        if not _beats(kind, cand.value, prev):
            continue
        rec = PersonalRecord(
            user_id=user_id, kind=kind, value=cand.value, previous_value=prev,
            activity_id=cand.activity_id, date=cand.date,
        )
        session.add(rec)
        inserted.append(rec)
    return inserted


def announce_worthy(records: list, today: Optional[dt.date] = None) -> list:
    """Filter freshly-detected records down to those achieved in the last ``FRESH_DAYS`` —
    a backfill dates its bests in the past, so this stays silent for it."""
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=FRESH_DAYS)).isoformat()
    return [r for r in records if r.date and r.date >= cutoff]


# ---------- FORMATTING ----------

def _fmt_pace(min_km: float) -> str:
    return fmt.pace(min_km, "/км")


def _fmt_hms(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_value(kind: str, value: float) -> str:
    if kind in _PACE_KINDS:
        return _fmt_pace(value)
    if kind in _RACE_KINDS:
        return _fmt_hms(value)
    if kind == "vo2max":
        return f"{value:.0f}"
    if kind == "longest_run_min":
        return f"{value:.0f} хв"
    return f"{value:.1f} км"   # longest_run_km, biggest_week_km


def format_record_line(rec, *, with_prev: bool = True) -> str:
    label = LABELS.get(rec.kind, rec.kind)
    line = f"🏅 {label}: {format_value(rec.kind, rec.value)}"
    if with_prev and rec.previous_value is not None:
        line += f" (було {format_value(rec.kind, rec.previous_value)})"
    return line


def celebrate(records: list) -> str:
    """The '🎉 Новий рекорд' message for one or more freshly-set records."""
    head = "🎉 Новий особистий рекорд!" if len(records) == 1 else "🎉 Нові особисті рекорди!"
    lines = [format_record_line(r) for r in records]
    return head + "\n" + "\n".join(lines)


def to_context(records: list) -> list:
    """Compact dicts for the Claude context (morning report / digest) + the dedup-cache key."""
    return [
        {k: v for k, v in {
            "kind": r.kind,
            "label": LABELS.get(r.kind, r.kind),
            "value": format_value(r.kind, r.value),
            "previous": (format_value(r.kind, r.previous_value)
                         if r.previous_value is not None else None),
            "date": r.date,
        }.items() if v is not None}
        for r in records
    ]
