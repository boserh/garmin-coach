"""Per-user data view — a logged-in user browses their own metrics, activities and
reports (scoped to their user_id). Mirrors the admin /ui browser but never spans
other users, and excludes the users / bot_state tables."""
import csv
import datetime as dt
import io
import json
import logging
import math
import time as _time
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, nullslast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import dayview, stepmatch, subjective
from app import format as fmt
from app.banners import banner
from app.charts import run_charts as _run_charts
from app.charts import shade_zones as _shade_zones
from app.charts import trend_series as _trend_series
from app.core.auth import current_user
from app.core.tz import user_today
from app.db import lifestyle as lifestyle_db
from app.db.models import (
    ActivityRecord,
    DailyMetric,
    PersonalRecord,
    PlannedWorkout,
    ReportLog,
    TrainingPlan,
    User,
)
from app.dependencies import get_session
from app.garmin import repository, service
from app.garmin.runtime import user_runtime
from app.routers.admin import INDEX_COLS
from app.templating import create_templates

logger = logging.getLogger("api")

templates = create_templates()


def _hm(hours):
    """Decimal hours → 'Xг Yхв' (8.6 → '8 год 36 хв'); empty for None."""
    if hours is None:
        return ""
    total = round(hours * 60)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


templates.env.filters["hm"] = _hm
templates.env.filters["sets_word"] = fmt.sets_word

# Only the user's own data tables (all carry user_id).
TABLES = {
    "daily_metrics": DailyMetric,
    "activities": ActivityRecord,
    "report_logs": ReportLog,
}

router = APIRouter(tags=["me"])


async def _count(session: AsyncSession, model, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
    ).scalar_one()


# ---- activities: a nice card view (type icon, key stats, run sparkline) ----
# activity type → (emoji, accent colour). Matched exactly, else by first word.
_ACT_META = {
    "running": ("🏃", "#7aa2f7"), "treadmill_running": ("🏃", "#7aa2f7"),
    "trail_running": ("⛰️", "#9ece6a"), "track_running": ("🏃", "#7aa2f7"),
    "walking": ("🚶", "#73daca"), "hiking": ("🥾", "#9ece6a"),
    "cycling": ("🚴", "#7dcfff"), "road_biking": ("🚴", "#7dcfff"),
    "mountain_biking": ("🚵", "#9ece6a"), "indoor_cycling": ("🚴", "#7dcfff"),
    "gravel_cycling": ("🚵", "#e0af68"), "gravel_ride": ("🚵", "#e0af68"),
    "strength_training": ("🏋️", "#e0af68"), "cardio": ("❤️", "#f7768e"),
    "yoga": ("🧘", "#bb9af7"), "swimming": ("🏊", "#7dcfff"),
    "lap_swimming": ("🏊", "#7dcfff"), "kitesurfing": ("🪁", "#7dcfff"),
    "kiteboarding": ("🪁", "#7dcfff"), "kiteboarding_v2": ("🪁", "#7dcfff"),
    "tennis": ("🎾", "#c3e88d"), "tennis_v2": ("🎾", "#c3e88d"),
    # street / virtual
    "street_running": ("🏃", "#7aa2f7"), "virtual_run": ("🏃", "#7aa2f7"),
    "ultra_run": ("🏔️", "#9ece6a"), "indoor_walking": ("🚶", "#73daca"),
    "virtual_ride": ("🚴", "#7dcfff"),
    # strength / gym
    "hiit": ("🔥", "#f7768e"), "jump_rope": ("🪢", "#f7768e"),
    "pilates": ("🤸", "#bb9af7"), "functional_training": ("🏋️", "#e0af68"),
    "gymnastics": ("🤸", "#bb9af7"),
    # mind / breathing
    "meditation": ("🧘", "#bb9af7"), "breathing": ("🌬️", "#bb9af7"),
    # water
    "open_water_swimming": ("🌊", "#7dcfff"),
    "surfing": ("🏄", "#7dcfff"), "surfing_v2": ("🏄", "#7dcfff"),
    "stand_up_paddleboarding": ("🏄", "#73daca"),
    "stand_up_paddleboarding_v2": ("🏄", "#73daca"),
    "rowing": ("🚣", "#7dcfff"), "kayaking": ("🛶", "#7dcfff"),
    "sailing": ("⛵", "#7dcfff"),
    # snow / ice
    "resort_skiing": ("⛷️", "#7dcfff"), "downhill_skiing": ("⛷️", "#7dcfff"),
    "cross_country_skiing": ("⛷️", "#9ece6a"), "backcountry_skiing": ("⛷️", "#9ece6a"),
    "snowboarding": ("🏂", "#7dcfff"),
    "skating_ws": ("⛸️", "#7dcfff"), "inline_skating": ("🛼", "#73daca"),
    # court / team
    "pickleball": ("🏓", "#c3e88d"), "table_tennis": ("🏓", "#c3e88d"),
    "basketball": ("🏀", "#e0af68"), "volleyball": ("🏐", "#e0af68"),
    "soccer": ("⚽", "#9ece6a"), "football": ("🏈", "#e0af68"),
    "badminton": ("🏸", "#c3e88d"), "squash": ("🎾", "#c3e88d"),
    # climbing
    "indoor_climbing": ("🧗", "#9ece6a"), "bouldering": ("🧗", "#9ece6a"),
    "rock_climbing": ("🧗", "#9ece6a"),
    # other
    "golf": ("⛳", "#9ece6a"), "boxing": ("🥊", "#f7768e"),
    "martial_arts": ("🥋", "#f7768e"),
}
_RUNWALK = {"running", "treadmill_running", "trail_running", "track_running",
            "walking", "hiking"}

_TYPE_LABELS: dict[str, str] = {
    # running
    "running": "Біг", "treadmill_running": "Біг (доріжка)",
    "trail_running": "Трейл", "track_running": "Біг (трек)",
    "street_running": "Стріт ран", "virtual_run": "Віртуальний біг",
    "ultra_run": "Ультра",
    # walking / hiking
    "walking": "Ходьба", "hiking": "Хайкінг", "indoor_walking": "Ходьба",
    # cycling
    "cycling": "Велосипед", "road_biking": "Шосе",
    "mountain_biking": "МТБ", "indoor_cycling": "Велотренажер",
    "gravel_cycling": "Гравел", "gravel_ride": "Гравел",
    "virtual_ride": "Велосипед",
    # strength / gym
    "strength_training": "Сила", "cardio": "Кардіо",
    "hiit": "HIIT", "jump_rope": "Скакалка",
    "pilates": "Пілатес", "functional_training": "Функціональне",
    "gymnastics": "Гімнастика",
    # mind / flexibility
    "yoga": "Йога", "meditation": "Медитація", "breathing": "Дихання",
    # water
    "swimming": "Плавання", "lap_swimming": "Плавання",
    "open_water_swimming": "Відкрита вода",
    "kitesurfing": "Кайт", "kiteboarding": "Кайт", "kiteboarding_v2": "Кайт",
    "surfing": "Серфінг", "surfing_v2": "Серфінг",
    "stand_up_paddleboarding": "SUP", "stand_up_paddleboarding_v2": "SUP",
    "rowing": "Веслування", "kayaking": "Каяк", "sailing": "Вітрила",
    # snow / ice
    "resort_skiing": "Гірські лижі", "downhill_skiing": "Гірські лижі",
    "cross_country_skiing": "Бігові лижі", "backcountry_skiing": "Бекантрі",
    "snowboarding": "Сноуборд",
    "skating_ws": "Ковзани", "inline_skating": "Ролики",
    # court / team
    "tennis": "Теніс", "tennis_v2": "Теніс",
    "pickleball": "Піклбол", "table_tennis": "Настільний теніс",
    "basketball": "Баскетбол", "volleyball": "Волейбол",
    "soccer": "Футбол", "football": "Американський футбол",
    "badminton": "Бадмінтон", "squash": "Сквош",
    # climbing
    "indoor_climbing": "Скеледром", "bouldering": "Боулдеринг",
    "rock_climbing": "Скелі",
    # other
    "golf": "Гольф", "boxing": "Бокс",
    "martial_arts": "Єдиноборства",
}

_SORT_OPTIONS = [
    ("date_desc",  "Дата ↓"),
    ("date_asc",   "Дата ↑"),
    ("dist_desc",  "Відстань ↓"),
    ("dur_desc",   "Тривалість ↓"),
    ("load_desc",  "Навантаження ↓"),
    ("hr_desc",    "Пульс ↓"),
]

_DAYS_LABELS = {30: "30 днів", 90: "3 міс", 365: "Рік"}


def _filter_summary(type_counts, type_filter="", days_filter=0, sort="date_desc",
                    date_from="", date_to=""):
    """What the activity list is currently filtered by, as ``(label, is_active)``.

    The filter bar is a long wall of sport pills (one per activity type ever recorded —
    two full phone screens before the first activity), so it renders collapsed. The
    label is what a collapsed bar has to say instead: "Фільтри" when nothing is applied,
    else the active choices spelled out. ``is_active`` also decides whether the bar opens
    itself — a filtered list must never look like the whole history.
    """
    bits = []
    if type_filter:
        tc = next((t for t in type_counts if t["type"] == type_filter), None)
        bits.append(f"{tc['emoji']} {tc['label']}" if tc else type_filter)
    if date_from or date_to:
        bits.append(" – ".join(p for p in (date_from, date_to) if p))
    elif days_filter:
        bits.append(_DAYS_LABELS.get(days_filter, f"{days_filter} днів"))
    if sort != "date_desc":
        bits.append(dict(_SORT_OPTIONS).get(sort, sort))
    return ("Фільтри · " + " · ".join(bits) if bits else "Фільтри"), bool(bits)
def _act_meta(t: str):
    t = (t or "").lower()
    if t in _ACT_META:
        return _ACT_META[t]
    head = t.split("_")[0]
    for k, v in _ACT_META.items():
        if k.startswith(head):
            return v
    return ("🏅", "#909aa8")


def _nice_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat((iso or "")[:10])
        return f"{fmt.WEEKDAYS_UK[d.weekday()]}, {fmt.day_month(d)} {d.year}"
    except (ValueError, TypeError):
        return iso or ""


def _pace_str(dist_km, dur_min):
    if not dist_km or not dur_min:
        return None
    return fmt.pace(dur_min / dist_km)   # seconds per km → M:SS


def act_label(type_str) -> str:
    """The activity type's Ukrainian name. An unmapped Garmin slug degrades to a readable
    form ("Open water swimming") rather than being shown raw — the dashboard used to print
    `stand_up_paddleboarding_v2` at the user."""
    t = (type_str or "").lower()
    return _TYPE_LABELS.get(t) or t.replace("_", " ").capitalize()


def act_pace(type_str, dist_km, dur_min):
    """Pace, but only where pace means anything. Distance ÷ duration is a number for any
    activity, and printing it as "22:52 /км" under a paddleboard session is worse than
    printing nothing — a ride is read in km/h, and a SUP session in neither."""
    if (type_str or "").lower() not in _RUNWALK:
        return None
    return _pace_str(dist_km, dur_min)


def _fmt_num(v):
    """22.0 → '22', 22.5 → '22.5' — drop a trailing .0 so weights read cleanly."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _summ(vals):
    """Summarise a per-set list (reps or weights) to a single value or a lo–hi range,
    ignoring None entries. None when there's nothing to show."""
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    lo, hi = min(xs), max(xs)
    return _fmt_num(lo) if lo == hi else f"{_fmt_num(lo)}–{_fmt_num(hi)}"


def _exercise_rows(exercises):
    """Format a stored ``exercises`` dict into display cards ``[{name, sets, detail}]`` — set
    count, reps and weight per exercise, for the Garmin-style card view. Handles the current
    per-set shape ({count, reps[], weight_kg[]}) and the legacy shape (a bare set-count int),
    so activities synced before reps/weight were captured still render (just the count)."""
    if not exercises:
        return []
    out = []
    for name, info in (exercises.get("sets") or {}).items():
        if isinstance(info, dict):
            count = info.get("count") or 0
            reps = _summ(info.get("reps") or [])
            wv = info.get("weight_kg") or []
            weight = (_summ(wv) + " кг") if any(v is not None for v in wv) else "власна вага"
            parts = ([f"{reps} повт."] if reps else []) + [weight]
            out.append({"name": name, "sets": count, "detail": " · ".join(parts)})
        else:
            out.append({"name": name, "sets": info, "detail": ""})   # legacy: count only
    return out


def _spark(series, n: int = 48):
    """A pace sparkline (SVG points) from a run's series; faster = higher. None if too short."""
    vals = [p.get("p") for p in (series or []) if p.get("p")]
    if len(vals) < 3:
        return None
    if len(vals) > n:
        step = len(vals) / n
        vals = [vals[int(i * step)] for i in range(n)]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    W, H, pad = 160, 36, 3
    m = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (W - 2 * pad) * i / (m - 1)
        y = pad + (H - 2 * pad) * (v - lo) / rng   # higher pace (slower) sits lower
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _act_stmt(user_id, type_filter="", days_filter=0, sort="date_desc",
              date_from="", date_to=""):
    stmt = select(ActivityRecord).where(
        ActivityRecord.user_id == user_id,
        ActivityRecord.is_hidden.is_(False),   # ST-17
    )
    if type_filter:
        stmt = stmt.where(ActivityRecord.type == type_filter)
    if date_from or date_to:
        if date_from:
            stmt = stmt.where(ActivityRecord.date >= date_from)
        if date_to:
            stmt = stmt.where(ActivityRecord.date <= date_to)
    elif days_filter > 0:
        since = (dt.date.today() - dt.timedelta(days=days_filter)).isoformat()
        stmt = stmt.where(ActivityRecord.date >= since)
    order = {
        "date_desc": [ActivityRecord.date.desc(), ActivityRecord.id.desc()],
        "date_asc":  [ActivityRecord.date.asc(),  ActivityRecord.id.asc()],
        "dist_desc": [nullslast(ActivityRecord.dist_km.desc()), ActivityRecord.date.desc()],
        "dur_desc":  [nullslast(ActivityRecord.dur_min.desc()), ActivityRecord.date.desc()],
        "load_desc": [nullslast(ActivityRecord.load.desc()),    ActivityRecord.date.desc()],
        "hr_desc":   [nullslast(ActivityRecord.avg_hr.desc()),  ActivityRecord.date.desc()],
    }.get(sort, [ActivityRecord.date.desc(), ActivityRecord.id.desc()])
    return stmt.order_by(*order)


async def _activity_cards(session, user_id, limit, offset,
                          type_filter="", days_filter=0, sort="date_desc",
                          date_from="", date_to=""):
    rows = (await session.execute(
        _act_stmt(user_id, type_filter, days_filter, sort,
                  date_from, date_to).limit(limit).offset(offset)
    )).scalars().all()
    cards = []
    for r in rows:
        emoji, color = _act_meta(r.type)
        runwalk = (r.type or "").lower() in _RUNWALK
        strain_ring = {"color": "#3aa0ff", **_ring_geom(r.load / 2, 24)} if r.load else None
        cards.append({
            "id": r.id, "emoji": emoji, "color": color, "label": act_label(r.type),
            "date": _nice_date(r.date),
            "dist_km": r.dist_km, "dur_min": r.dur_min,
            "avg_hr": r.avg_hr, "max_hr": r.max_hr, "load": r.load,
            "pace": act_pace(r.type, r.dist_km, r.dur_min),
            "spark": _spark(r.series) if runwalk else None,
            "strain_ring": strain_ring,
            "has_analysis": bool(r.analysis),
            "rpe": (r.subjective or {}).get("rpe"),
            "pain": (r.subjective or {}).get("note") or (r.subjective or {}).get("pain"),
        })
    return cards


async def _activity_count_filtered(session, user_id, type_filter="", days_filter=0,
                                    date_from="", date_to=""):
    stmt = (select(func.count()).select_from(ActivityRecord)
            .where(ActivityRecord.user_id == user_id,
                   ActivityRecord.is_hidden.is_(False)))   # ST-17
    if type_filter:
        stmt = stmt.where(ActivityRecord.type == type_filter)
    if date_from or date_to:
        if date_from:
            stmt = stmt.where(ActivityRecord.date >= date_from)
        if date_to:
            stmt = stmt.where(ActivityRecord.date <= date_to)
    elif days_filter > 0:
        since = (dt.date.today() - dt.timedelta(days=days_filter)).isoformat()
        stmt = stmt.where(ActivityRecord.date >= since)
    return (await session.execute(stmt)).scalar_one()


async def _activity_type_counts(session, user_id, days_filter=0, date_from="", date_to=""):
    """Returns list of (type, count) sorted by count desc, respecting date filter."""
    stmt = (
        select(ActivityRecord.type, func.count().label("n"))
        .where(ActivityRecord.user_id == user_id,
               ActivityRecord.is_hidden.is_(False))   # ST-17
    )
    if date_from:
        stmt = stmt.where(ActivityRecord.date >= date_from)
    if date_to:
        stmt = stmt.where(ActivityRecord.date <= date_to)
    elif days_filter:
        cutoff = (dt.date.today() - dt.timedelta(days=days_filter)).isoformat()
        stmt = stmt.where(ActivityRecord.date >= cutoff)
    stmt = stmt.group_by(ActivityRecord.type).order_by(func.count().desc())
    rows = (await session.execute(stmt)).all()
    return [
        {"type": t, "count": n, "emoji": _act_meta(t)[0], "label": act_label(t)}
        for t, n in rows if t
    ]


# ---- daily recovery metrics ----
_RING_R = 76
_RING_CIRC = round(2 * math.pi * _RING_R, 1)


def _recovery_band(v):
    """Whoop-style recovery zone for a 0–100 score → (colour, label)."""
    if v is None:
        return "#6b7490", "—"
    if v >= 67:
        return "#16e08a", "Відновлено"
    if v >= 34:
        return "#ffd23f", "Помірно"
    return "#ff5470", "Втома"


def _ring_geom(value, r):
    """SVG ring dash/circumference for a 0–100 ``value`` at radius ``r``."""
    circ = round(2 * math.pi * r, 1)
    return {"circ": circ, "dash": round(circ * min(max(value, 0), 100) / 100, 1), "r": r}


def _recovery_ring(day):
    """Hero ring model from the latest day: readiness if present, else sleep score."""
    val = day["readiness"] if day.get("readiness") is not None else day.get("sleep_score")
    if val is None:
        return None
    color, label = _recovery_band(val)
    return {
        "value": int(val), "color": color, "label": label,
        "metric": "готовність" if day.get("readiness") is not None else "сон, бал",
        "circ": _RING_CIRC, "dash": round(_RING_CIRC * min(val, 100) / 100, 1),
        "r": _RING_R, "date": day["date"],
        "sleep_hm": _hm(day.get("sleep_h")), "hrv_avg": day.get("hrv_avg"), "rhr": day.get("rhr"),
    }


async def _daily_trends(session, user_id, days: int = 60):
    """HRV / sleep-hours / sleep-score trend charts (hover-enabled) for the daily view."""
    trend = await repository.read_history(session, user_id, days=days)
    dates = [r["date"] for r in trend]
    defs = [
        ("HRV avg", "#7aa2f7", "int", [r["hrv_avg"] for r in trend]),
        ("Сон, год", "#9ece6a", "f1", [r["sleep_h"] for r in trend]),
        ("Сон, бал", "#e0af68", "int", [r["sleep_score"] for r in trend]),
    ]
    charts = [{"label": lbl, "color": c, "fmt": fmt, "s": s}
              for lbl, c, fmt, vals in defs if (s := _trend_series(vals, dates))]
    return charts, (dates[0] if dates else ""), (dates[-1] if dates else "")



def _hrv_color(status):
    s = (status or "").upper()
    if s == "BALANCED":
        return "#9ece6a"
    if s in ("UNBALANCED", "LOW", "POOR"):
        return "#f7768e"
    return "#e0af68"


def _fmt_race(s) -> str:
    if not isinstance(s, (int, float)) or s <= 0:
        return str(s)
    t = int(s)
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_dist(m) -> str:
    if not isinstance(m, (int, float)):
        return str(m)
    return f"{m / 1000:.1f} км" if m >= 1000 else f"{int(m)} м"


# Keys the day page renders somewhere other than a label/value row — in the hero, a
# band gauge, the sleep bar. Listing them here is what keeps them out of "Інше" twice.
_SKIP_KEYS = frozenset({
    "resting_hr", "readiness_score", "auto_activities",
    "sleep_feedback", "hrs_feedback", "hrv_feedback",
    "readiness_feedback", "readiness_level", "acwr_feedback", "endurance_class",
    "hrv_5day_high",
    # drawn as geometry, not text
    "hrv_avg", "hrv_baseline_low", "hrv_baseline_high", "hrv_weekly_avg", "hrv_5min_high",
    "sleep_need_h", "sleep_need_feedback", "bb_high", "bb_low", "bb_change",
    "steps", "distance_m",
})


def _fmt_dev(v) -> str:
    """Skin-temperature deviation: the sign is the whole point of the number."""
    return f"{v:+.1f}" if isinstance(v, (int, float)) else str(v)


# Groups the template lays out as cards. The keys are the ones ``_daily_extra`` /
# ``_daily_extra_metrics`` actually write — several of these used to be guesses
# (``calories``, ``floors``, ``body_battery_change``), so the real values fell through
# to an "Інше" dump of raw English key names at the bottom of the page.
_GROUPS = {
    "sleep": [
        ("avg_hr_sleep",      "пульс уві сні",     None),
        ("overnight_hrv",     "HRV за ніч",        None),
        ("awake_count",       "пробуджень",         None),
        ("restless_moments",  "неспокійні моменти", None),
        ("avg_sleep_stress",  "стрес уві сні",      None),
        ("spo2_avg",          "SpO₂, сер. %",       None),
        ("spo2_low",          "SpO₂, мін. %",       None),
        ("respiration_avg",   "дихання, /хв",       None),
        ("skin_temp_dev_c",   "темп. шкіри, °C",    _fmt_dev),
        ("breathing_disruption_sev", "збої дихання", dayview.humanize),
    ],
    "load": [
        ("recovery_time_h",   "відновлення, год",   None),
        ("acute_load",        "гостре навантаження", None),
        ("acwr_pct",          "ACWR, %",            None),
    ],
    "activity": [
        ("active_kcal",       "активні ккал",       None),
        ("moderate_min",      "помірна, хв",        None),
        ("vigorous_min",      "інтенсивна, хв",     None),
        ("floors_up",         "поверхів угору",     None),
        ("min_hr",            "мін. пульс",         None),
    ],
    "predictions": [
        ("race_5k_s",         "5 км",               _fmt_race),
        ("race_10k_s",        "10 км",              _fmt_race),
        ("race_half_s",       "напівмарафон",       _fmt_race),
        ("race_marathon_s",   "марафон",            _fmt_race),
        ("vo2max",            "VO₂max",             None),
        ("endurance_score",   "витривалість",       None),
    ],
}


def _rows(ex: dict, group: str) -> list:
    return [
        {"label": lbl, "value": f(ex[k]) if f else ex[k]}
        for k, lbl, f in _GROUPS[group] if ex.get(k) is not None
    ]


def _leftover_rows(ex: dict) -> list:
    """Anything the watch sent that no group claims — shown de-shouted rather than as a
    raw ``sleep_need_feedback: HIGHLY_INCREASED`` line."""
    known = {k for fields in _GROUPS.values() for k, *_ in fields} | _SKIP_KEYS
    return [
        {"label": k.replace("_", " "), "value": dayview.humanize(v) if isinstance(v, str) else v}
        for k, v in sorted(ex.items())
        if k not in known and v is not None and isinstance(v, (int, float, str))
    ]


async def _prior_days(session, user_id: int, before: str, days: int = 45) -> list:
    """The stored days immediately *before* the one being viewed — the sample every band
    on the page is drawn against. Anchored on the viewed date, not on today: opening a
    day from March must compare it with March, not with the last six weeks."""
    start = ""
    try:
        d = dt.date.fromisoformat((before or "")[:10])
        start = (d - dt.timedelta(days=days)).isoformat()
    except (ValueError, TypeError):
        return []
    return list((await session.execute(
        select(DailyMetric)
        .where(DailyMetric.user_id == user_id,
               DailyMetric.date < before, DailyMetric.date >= start)
        .order_by(DailyMetric.date)
    )).scalars().all())


def _col(rows, name: str) -> list:
    """One metric's history, reading through ``extra`` for the keys with no column."""
    return [getattr(r, name, None) if hasattr(r, name) else (r.extra or {}).get(name)
            for r in rows]


def _ex_col(rows, key: str) -> list:
    return [(r.extra or {}).get(key) for r in rows]


def _delta_text(delta, *, hours: bool = False) -> str:
    """A signed difference against the personal median, in the metric's own units."""
    if delta is None:
        return ""
    if hours:
        mins = round(delta * 60)
        if mins == 0:
            return "як зазвичай"
        return ("+" if mins > 0 else "−") + _hm(abs(mins) / 60)
    if abs(delta) < 0.05:
        return "як зазвичай"
    val = round(delta, 1)
    val = int(val) if float(val).is_integer() else val
    return f"+{val}" if delta > 0 else f"−{abs(val)}"


def _key_metrics(m, ex: dict, prior: list) -> list:
    """The five numbers the page leads with, each with its own personal band."""
    specs = [
        ("Сон, бал", m.sleep_score, _col(prior, "sleep_score"), False, False, ""),
        ("Тривалість сну", m.sleep_h, _col(prior, "sleep_h"), False, True, ""),
        ("HRV за ніч", m.hrv_avg, _col(prior, "hrv_avg"), False, False, "мс"),
        ("Пульс спокою", ex.get("resting_hr"), _ex_col(prior, "resting_hr"), True, False, ""),
        ("Стрес, середній", m.stress_avg, _col(prior, "stress_avg"), True, False, ""),
    ]
    out = []
    for label, value, history, lower_better, hours, unit in specs:
        if value is None:
            continue
        # HRV is the one metric with a manufacturer-supplied normal range; everything
        # else infers one from this person's own recent weeks.
        if label.startswith("HRV"):
            g = dayview.hrv_gauge(
                value,
                baseline_low=ex.get("hrv_baseline_low"), baseline_high=ex.get("hrv_baseline_high"),
                weekly_avg=ex.get("hrv_weekly_avg"), night_high=ex.get("hrv_5min_high"),
            ) or dayview.history_gauge(history, value)
        else:
            g = dayview.history_gauge(history, value, lower_better=lower_better)
        out.append({
            "label": label, "unit": unit,
            "value": _hm(value) if hours else value,
            "gauge": g,
            "delta": _delta_text(g.get("delta"), hours=hours) if g else "",
        })
    return out


def _day_view(m, ex: dict, prior: list) -> dict:
    """Everything the day template paints, computed once here so the markup stays markup."""
    score = ex.get("readiness_score")
    metric = "готовність"
    if score is None:
        score, metric = m.sleep_score, "сон, бал"
    hero = None
    if score is not None:
        color, band_label = _recovery_band(score)
        hero = {"value": int(score), "color": color, "band": band_label, "metric": metric,
                **_ring_geom(score, _RING_R)}
    verdict = ex.get("readiness_feedback") or ex.get("sleep_feedback") or ex.get("hrv_feedback")
    need = ex.get("sleep_need_h")
    return {
        "hero": hero,
        "verdict": dayview.humanize(verdict) if verdict else "",
        "metrics": _key_metrics(m, ex, prior),
        "sleep": {
            "total": m.sleep_h,
            "segments": dayview.sleep_segments(
                deep=m.deep_h, rem=m.rem_h, light=m.light_h, awake=m.awake_h),
            "start": ex.get("sleep_start"), "end": ex.get("sleep_end"),
            "need": need,
            "need_feedback": dayview.humanize(ex["sleep_need_feedback"])
            if ex.get("sleep_need_feedback") else "",
            "need_bar": dayview.ratio_bar(m.sleep_h, need),
            "rows": _rows(ex, "sleep"),
        },
        "battery": {
            "span": dayview.battery_span(ex.get("bb_low"), ex.get("bb_high")),
            "charged": m.bb_charged, "drained": m.bb_drained,
            "change": ex.get("bb_change"), "stress_max": m.stress_max,
        },
        "activity": {
            "steps": ex.get("steps"),
            "steps_gauge": dayview.history_gauge(_ex_col(prior, "steps"), ex.get("steps")),
            "distance": _fmt_dist(ex["distance_m"]) if ex.get("distance_m") is not None else "",
            "rows": _rows(ex, "activity"),
        },
        "load": _rows(ex, "load"),
        "predictions": _rows(ex, "predictions"),
        "other": _leftover_rows(ex),
        "sample": len(prior),
    }


async def _daily_cards(session, user_id, limit, offset):
    from app import completeness

    rows = (await session.execute(
        select(DailyMetric).where(DailyMetric.user_id == user_id)
        .order_by(DailyMetric.date.desc()).limit(limit).offset(offset)
    )).scalars().all()
    # ST-18: judge completeness against the fields this user actually produces (last 30 days),
    # so a metric they never have doesn't badge every day as "incomplete".
    expected = completeness.expected_fields(
        await repository.read_history(session, user_id, days=30)
    )
    out = []
    for r in rows:
        ex = r.extra or {}
        score = ex.get("readiness_score")
        if score is None:
            score = r.sleep_score
        ring = None
        if score is not None:
            color, _ = _recovery_band(score)
            ring = {"value": int(score), "color": color, **_ring_geom(score, 26)}
        out.append({
            "id": r.id, "date": _nice_date(r.date),
            "sleep_score": r.sleep_score, "sleep_h": r.sleep_h,
            "hrv_avg": r.hrv_avg, "hrv_status": r.hrv_status, "hrv_color": _hrv_color(r.hrv_status),
            "stress_avg": r.stress_avg,
            "bb_charged": r.bb_charged, "bb_drained": r.bb_drained,
            "rhr": ex.get("resting_hr"), "readiness": ex.get("readiness_score"),
            "auto_activities": ex.get("auto_activities"),
            "ring": ring,
            "incomplete": completeness.labels(completeness.daily_completeness(r, expected)),
        })
    return out


async def _latest_ring(session, user_id):
    """The recovery ring for the most recent day (for the /me overview hero)."""
    rows = await _daily_cards(session, user_id, 1, 0)
    return _recovery_ring(rows[0]) if rows else None


# ---- report history ----
_KIND_META = {
    "report": ("Звіт", "#7aa2f7"), "morning": ("Ранок", "#e0af68"),
    "deep": ("Глибокий", "#bb9af7"), "ask": ("Питання", "#7dcfff"),
    "activity": ("Активність", "#9ece6a"), "plan": ("План", "#bb9af7"),
    "plan_edit": ("Правка", "#73daca"),
}


def _kind_meta(k):
    return _KIND_META.get(k, (k or "—", "#909aa8"))


async def _report_cards(session, user_id, limit, offset):
    rows = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == user_id)
        .order_by(ReportLog.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    out = []
    for r in rows:
        label, color = _kind_meta(r.kind)
        out.append({
            "id": r.id, "label": label, "color": color, "ok": r.ok, "cached": r.cached,
            "when": r.created_at.strftime("%d.%m %H:%M") if r.created_at else "",
            "cost": r.cost_usd, "in_tok": r.input_tokens, "out_tok": r.output_tokens,
            "preview": ((r.report_text or r.question or r.error or "").strip()[:140]),
        })
    return out


# ---- NF-13: GET /me/export — a streamed ZIP of everything this account owns ----
# Column lists are explicit (not `__table__.columns`) so a future secret-bearing column
# on one of these models can never leak into an export by accident.
_EXPORT_DAILY_COLS = [
    "id", "date", "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
    "extra", "created_at", "updated_at",
]
_EXPORT_DAILY_CSV_COLS = [c for c in _EXPORT_DAILY_COLS if c != "extra"]

_EXPORT_ACTIVITY_COLS = [
    "id", "activity_id", "date", "type", "dur_min", "dist_km", "avg_hr", "max_hr", "load",
    "exercises", "series", "analysis", "subjective", "step_match", "route_id", "created_at",
]

# NF-33: the recognised routes themselves. Exported because they're derived from the user's
# own tracks and are what makes their activity history readable ("route 3" means nothing
# without this file); the fingerprint is a coarse signature, not a reconstructable track.
_EXPORT_ROUTE_COLS = ["id", "name", "fingerprint", "created_at"]
_EXPORT_ACTIVITY_CSV_COLS = [
    c for c in _EXPORT_ACTIVITY_COLS
    if c not in ("exercises", "series", "subjective", "step_match")
]

_EXPORT_RECORD_COLS = ["id", "kind", "value", "previous_value", "activity_id", "date", "created_at"]

_EXPORT_REPORT_COLS = [
    "id", "created_at", "kind", "model", "input_tokens", "output_tokens", "cost_usd",
    "ok", "cached", "error", "question", "report_text", "tool_rounds",
]

_EXPORT_PLAN_COLS = [
    "id", "goal", "goal_label", "target_date", "start_date", "days_per_week",
    "intensity", "intake", "summary", "status", "created_at",
]

_EXPORT_WORKOUT_COLS = [
    "id", "date", "week", "type", "dist_km", "description", "steps",
    "garmin_workout_id", "garmin_schedule_id", "garmin_template_id", "exercise_edits",
    "strength_plan", "strength_snapshot", "completed_activity_id", "match_info",
    "status", "created_at", "updated_at",
]


def _export_row(obj, cols: list) -> dict:
    out = {}
    for c in cols:
        v = getattr(obj, c)
        out[c] = v.isoformat() if isinstance(v, dt.datetime) else v
    return out


def _export_csv(rows: list, cols: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


@router.get("/me/export")
async def me_export(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """NF-13: a streamed ZIP of everything this account owns — full-fidelity JSON (extra/
    series/steps/subjective, nothing flattened away) plus flat CSV twins of the two tabular
    tables for Excel/Sheets. Pure DB read scoped to ``user.id``; the ``users`` row itself is
    never read, so credentials/garth token/password hash can't leak by construction. This is
    portability, not disaster recovery — OPS-02's DB backup stays the restore mechanism."""
    daily = (await session.execute(
        select(DailyMetric).where(DailyMetric.user_id == user.id).order_by(DailyMetric.date)
    )).scalars().all()
    activities = (await session.execute(
        select(ActivityRecord).where(ActivityRecord.user_id == user.id)
        .order_by(ActivityRecord.date)
    )).scalars().all()
    records = (await session.execute(
        select(PersonalRecord).where(PersonalRecord.user_id == user.id)
        .order_by(PersonalRecord.date)
    )).scalars().all()
    plans = (await session.execute(
        select(TrainingPlan).where(TrainingPlan.user_id == user.id).order_by(TrainingPlan.id)
    )).scalars().all()
    reports = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == user.id).order_by(ReportLog.created_at)
    )).scalars().all()

    daily_rows = [_export_row(m, _EXPORT_DAILY_COLS) for m in daily]
    activity_rows = [_export_row(a, _EXPORT_ACTIVITY_COLS) for a in activities]
    record_rows = [_export_row(r, _EXPORT_RECORD_COLS) for r in records]
    report_rows = [_export_row(r, _EXPORT_REPORT_COLS) for r in reports]
    # NF-28: the lifestyle tags are user-AUTHORED data (not derived cache), so they belong
    # in a portability export more clearly than anything else here.
    lifestyle_rows = await lifestyle_db.read_all(session, user.id)
    # EP-18: the coach's memory of this athlete is theirs too — and it is the most sensitive
    # text we hold, so a portability export that silently omitted it would be dishonest.
    from app.db import profile as profile_db
    profile_facts, _profile_stoplist = await profile_db.get_profile(session, user.id)
    # NF-34: declared away periods — the athlete's own words about their own life, so the
    # same portability argument as the lifestyle log applies.
    from app.db import away as away_db_export
    away_rows = await away_db_export.read_all(session, user.id)
    # NF-33: routes (an AC of the ticket) — user-scoped like everything else here.
    from app.garmin.repository import routes as routes_repo
    route_rows = [_export_row(r, _EXPORT_ROUTE_COLS)
                  for r in await routes_repo.list_routes(session, user.id)]

    plan_rows = []
    for p in plans:
        workouts = (await session.execute(
            select(PlannedWorkout).where(PlannedWorkout.plan_id == p.id)
            .order_by(PlannedWorkout.date)
        )).scalars().all()
        plan_rows.append({
            **_export_row(p, _EXPORT_PLAN_COLS),
            "workouts": [_export_row(w, _EXPORT_WORKOUT_COLS) for w in workouts],
        })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("daily_metrics.json", json.dumps(daily_rows, ensure_ascii=False, indent=2))
        zf.writestr("daily_metrics.csv", _export_csv(daily_rows, _EXPORT_DAILY_CSV_COLS))
        zf.writestr("activities.json", json.dumps(activity_rows, ensure_ascii=False, indent=2))
        zf.writestr("activities.csv", _export_csv(activity_rows, _EXPORT_ACTIVITY_CSV_COLS))
        zf.writestr("personal_records.json",
                    json.dumps(record_rows, ensure_ascii=False, indent=2))
        zf.writestr("plans.json", json.dumps(plan_rows, ensure_ascii=False, indent=2))
        zf.writestr("report_logs.json", json.dumps(report_rows, ensure_ascii=False, indent=2))
        zf.writestr("lifestyle_logs.json",
                    json.dumps(lifestyle_rows, ensure_ascii=False, indent=2))
        zf.writestr("athlete_profile.json",
                    json.dumps(profile_facts, ensure_ascii=False, indent=2))
        zf.writestr("routes.json", json.dumps(route_rows, ensure_ascii=False, indent=2))
        zf.writestr("away_periods.json", json.dumps(away_rows, ensure_ascii=False, indent=2))
    buf.seek(0)

    fname = f"bihun-export-{dt.date.today().isoformat()}.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---- ST-15: manual resync of one activity / a range of days ----

def _parse_resync_range(date_from: str, date_to: str):
    """Validate a resync range from the form. Returns ``(dates, error)`` — ``dates`` is the
    inclusive day list (ascending), ``error`` one of ``"format"``/``"range"`` or None. A
    missing ``date_to`` means a single day; a reversed range is swapped; a span above
    ``service.MAX_RESYNC_DAYS`` is rejected."""
    try:
        start = dt.date.fromisoformat(date_from)
        end = dt.date.fromisoformat(date_to) if date_to else start
    except (ValueError, TypeError):
        return None, "format"
    if end < start:
        start, end = end, start
    span = (end - start).days + 1
    if span > service.MAX_RESYNC_DAYS:
        return None, "range"
    return [start + dt.timedelta(days=i) for i in range(span)], None


@router.post("/me/activities/{row_id}/resync")
async def me_resync_activity(
    row_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """ST-15: re-pull one activity's summary/series/exercises from Garmin and overwrite the
    stored row (no duplicate). Runs in the user's Garmin runtime — an MFA gate propagates to
    the app-level handler (409 + "finish login in /settings"), not a stack trace. 404 if the
    id isn't this user's."""
    async with user_runtime(session, user):
        act = await service.resync_activity(session, user.id, row_id)
    if act is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    return RedirectResponse(f"/me/activities/{row_id}?resynced=1", status_code=303)


# ---- UI-04: the post-run check-in, in the browser ----
# ActivityRecord.subjective feeds half the analytics (EP-12 trend, NF-04's pain/RPE
# signals, NF-30, plan adaptation, the morning report), and the web could only READ it —
# the one place you'd naturally log it, right after a run, sent you to Telegram instead.
# Both entry points write through repository.set_subjective with the same vocabulary
# (app.subjective.PAIN_PARTS), so a knee logged here is the same knee logged in the bot.


@router.post("/me/activities/{row_id}/checkin")
async def me_activity_checkin(
    row_id: int,
    rpe: str = Form(""),
    pain: str = Form(""),        # a body-part slug, "none" for "no pain", "" for untouched
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Record RPE and/or a niggle for one activity. Idempotent by construction: a repeat
    tap overwrites the field via ``set_subjective`` (no second row, no history to fork)."""
    if user.is_demo:
        return RedirectResponse(f"/me/activities/{row_id}?checkin=demo", status_code=303)

    kwargs = {}
    if rpe:
        try:
            value = int(rpe)
        except ValueError:
            return RedirectResponse(f"/me/activities/{row_id}?checkin=bad", status_code=303)
        if not 1 <= value <= 10:
            return RedirectResponse(f"/me/activities/{row_id}?checkin=bad", status_code=303)
        kwargs["rpe"] = value
    if pain == "none":
        kwargs["pain"] = False
    elif pain:
        if pain not in subjective.PART_LABELS:
            return RedirectResponse(f"/me/activities/{row_id}?checkin=bad", status_code=303)
        kwargs["note"] = subjective.part_label(pain)
    if not kwargs:
        return RedirectResponse(f"/me/activities/{row_id}", status_code=303)

    act = await repository.set_subjective(session, user.id, row_id, **kwargs)
    if act is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    await session.commit()
    return RedirectResponse(f"/me/activities/{row_id}?checkin=ok", status_code=303)


@router.post("/me/lifestyle")
async def me_lifestyle(
    request: Request,
    date: str = Form(""),
    back: str = Form("/dashboard"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """NF-28's evening log, in the browser. Multi-select (beer AND a late meal is one
    evening), so the whole day is replaced by what's ticked — including an empty set,
    which is data ("nothing happened"), not an absent row."""
    if user.is_demo:
        return RedirectResponse(_safe_back(back, "?lifestyle=demo"), status_code=303)
    day = date or user_today(user).isoformat()
    form = await request.form()
    tags = [t for t in form.getlist("tags") if t in lifestyle_db.TAGS]
    await lifestyle_db.upsert(session, user.id, day, tags)
    return RedirectResponse(_safe_back(back, "?lifestyle=ok"), status_code=303)


def _safe_back(back: str, suffix: str) -> str:
    """Only ever redirect within this app — a form field is user input, and an open
    redirect off a POST is a phishing primitive."""
    if not back.startswith("/") or back.startswith("//"):
        back = "/dashboard"
    return f"{back}{suffix}"


@router.post("/me/resync-days")
async def me_resync_days(
    date_from: str = Form(...),
    date_to: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """ST-15: force-refetch ``daily_metrics`` for a range of days (hard-capped at
    ``service.MAX_RESYNC_DAYS``) and upsert over. Runs in the user's Garmin runtime (MFA →
    the app-level 409 flow). Redirects back to the daily view with a result banner."""
    dates, error = _parse_resync_range(date_from, date_to)
    if error:
        return RedirectResponse(f"/me/daily_metrics?resync_error={error}", status_code=303)
    async with user_runtime(session, user):
        written, requested = await service.resync_days(session, user.id, dates)
    return RedirectResponse(
        f"/me/daily_metrics?resynced={written}&of={requested}", status_code=303
    )


# ---- ST-19: regenerate a stored activity analysis (one paid, cache-bypassing re-run) ----

# In-process "not more than once a minute per activity" guard against a double-tap paying
# twice (per-process, best-effort — the button also disables itself on submit client-side).
_REGEN_MIN_INTERVAL_S = 60
_regen_guard: dict = {}


@router.post("/me/activities/{row_id}/regenerate")
async def me_regenerate_analysis(
    row_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """ST-19: regenerate one activity's Claude analysis, bypassing the dedup cache for a
    single paid re-run (after resynced data or a poor first write). Pure DB + Claude
    (``load_credentials``, no Garmin/MFA). A missing Claude key → a friendly banner; an
    ``AnalystError`` keeps the old text. Guarded against an accidental double-tap."""
    from app.analysis.reports import run_activity_analysis
    from app.analysis.service import AnalystError
    from app.garmin.credentials import load_credentials

    act = await repository.get_activity(session, user.id, row_id)
    if act is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    if user.is_demo:
        return RedirectResponse(f"/me/activities/{row_id}?regen=demo", status_code=303)
    creds = load_credentials(user)
    if not creds.anthropic_key:
        return RedirectResponse(f"/me/activities/{row_id}?regen=nokey", status_code=303)
    now = _time.monotonic()
    last = _regen_guard.get(row_id)
    if last is not None and now - last < _REGEN_MIN_INTERVAL_S:
        return RedirectResponse(f"/me/activities/{row_id}?regen=wait", status_code=303)
    _regen_guard[row_id] = now
    try:
        await run_activity_analysis(
            session, act, user_id=user.id, api_key=creds.anthropic_key, force=True
        )
        await session.commit()
    except AnalystError as e:
        logger.warning(f"REGEN activity user={user.id} id={row_id} failed: {e}")
        return RedirectResponse(f"/me/activities/{row_id}?regen=err", status_code=303)
    return RedirectResponse(f"/me/activities/{row_id}?regen=ok", status_code=303)


# ---- ST-17: hide / show an activity (dup / broken track) ----

@router.post("/me/activities/{row_id}/hide")
async def me_hide_activity(
    row_id: int,
    show: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """ST-17: hide (or, with ``show=1``, un-hide) one activity. Hidden activities vanish from
    every list / aggregate / record / plan-match and stay gone after the next Garmin sync
    (``upsert_activity`` never resets the flag). Pure DB, no Garmin/Claude. 404 if not
    this user's."""
    hidden = not (show == "1")
    act = await repository.set_activity_hidden(session, user.id, row_id, hidden)
    if act is None:
        raise HTTPException(status_code=404, detail="Activity not found")
    await session.commit()
    return RedirectResponse(
        f"/me/activities/{row_id}?{'shown' if not hidden else 'hidden'}=1", status_code=303
    )


@router.post("/me/activities/{row_id}/route-name")
async def me_rename_route(
    row_id: int,
    name: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """NF-33: name the route this activity belongs to ("парк", "робота і назад").

    The name is the only human-authored part of a route — everything else is derived — and it
    is what makes ``/compare route`` readable. Pure DB, no Garmin/Claude; 404 when the
    activity isn't this user's or carries no recognised route."""
    from app.garmin.repository import routes as routes_repo

    act = await repository.get_activity(session, user.id, row_id)
    if act is None or act.route_id is None:
        raise HTTPException(status_code=404, detail="Activity has no recognised route")
    await routes_repo.rename_route(session, user.id, act.route_id, name)
    await session.commit()
    return RedirectResponse(f"/me/activities/{row_id}?renamed=1", status_code=303)


@router.get("/me/jobs", response_class=HTMLResponse)
async def me_jobs(
    request: Request,
    job: str = Query(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """OPS-04: this user's last background-job runs (morning tick + the daily jobs) — when
    each ran, its result and reason. Pure DB read; a quick "чому щось не прийшло?" answer."""
    from app.db import job_runs as _job_runs
    runs = await _job_runs.recent_job_runs(session, user_id=user.id, job=job or None, limit=50)
    return templates.TemplateResponse(
        request, "jobs.html",
        {"runs": runs, "user": user, "base": "/me", "job_filter": job,
         "is_admin_view": False, "title": "Фонові задачі", "token": ""},
    )


@router.get("/me/profile", response_class=HTMLResponse)
async def me_profile(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """EP-18: everything the coach 'knows' about this athlete, with the reports each fact
    was drawn from. Transparency is not a nicety here — a profile the user cannot inspect is
    a profile they cannot correct, and an uncorrectable wrong fact quietly steers advice for
    months (the poisoning failure mode). Pure DB read, user-scoped."""
    from app import away as away_rules
    from app import profile as profile_rules
    from app.db import away as away_db
    from app.db import profile as profile_db

    facts, stoplist = await profile_db.get_profile(session, user.id)
    shown = profile_rules.select(facts)
    # NF-34 lives on this page because it is the same question — "what does the coach know
    # about my life?" — except this half the athlete writes themselves. Past periods are
    # kept visible for a while: they explain last week's numbers, which is exactly when
    # someone comes looking.
    today = user_today(user)
    periods = await away_db.list_periods(
        session, user.id, since=today - dt.timedelta(days=away_rules.RECENT_DAYS))
    for p in periods:
        p["status"] = away_rules.status(p, today)
        p["line"] = away_rules.describe(p)
    return templates.TemplateResponse(
        request, "profile.html",
        {"user": user, "facts": shown, "total": len(facts),
         "hidden": max(0, len(facts) - len(shown)), "stoplist": stoplist,
         "max_facts": profile_rules.MAX_FACTS,
         "away_periods": periods, "away_kinds": away_rules.KINDS,
         "away_kind_label": away_rules.label,
         "away_error": request.query_params.get("away_err"),
         "today": today.isoformat()},
    )


@router.post("/me/away")
async def me_away_add(
    request: Request,
    start: str = Form(""),
    end: str = Form(""),
    kind: str = Form(""),
    note: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """NF-34: declare an absence from the web. Same ``app.away.normalize`` bounds as the
    ``/away`` command and the plan-edit proposal — one validator, three doors."""
    from app import away as away_rules
    from app.db import away as away_db

    try:
        data = away_rules.normalize(start, end, kind, note, today=user_today(user))
    except away_rules.AwayError as e:
        return RedirectResponse(f"/me/profile?away_err={quote(str(e))}", status_code=303)
    await away_db.save(session, user.id, data)
    await session.commit()
    logger.info(f"AWAY stored user={user.id} {data['start_date']}..{data['end_date']} "
                f"kind={data['kind']} (from web)")
    return RedirectResponse("/me/profile", status_code=303)


@router.post("/me/away/{row_id}/delete")
async def me_away_delete(
    row_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Remove a declared period (plans change). User-scoped: another user's id simply
    isn't found."""
    from app.db import away as away_db

    await away_db.delete(session, user.id, row_id)
    await session.commit()
    return RedirectResponse("/me/profile", status_code=303)


@router.post("/me/profile/forget")
async def me_profile_forget(
    fact_id: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """"This isn't true": drop the fact AND stop-list it, so next week's pass cannot
    rediscover the same statement and quietly bring it back."""
    from app import profile as profile_rules
    from app.db import profile as profile_db

    facts, stoplist = await profile_db.get_profile(session, user.id)
    facts, stoplist, _removed = profile_rules.forget(facts, stoplist, fact_id)
    await profile_db.save_profile(session, user.id, facts, stoplist)
    await session.commit()
    return RedirectResponse("/me/profile", status_code=303)


@router.post("/me/profile/pin")
async def me_profile_pin(
    fact_id: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """"This matters": pin a fact so eviction and confidence decay can't drop it. The user
    overrode the heuristic; the heuristic must not quietly override them back."""
    from app.db import profile as profile_db

    facts, stoplist = await profile_db.get_profile(session, user.id)
    for f in facts:
        if f.get("id") == fact_id:
            f["pinned"] = not f.get("pinned")
    await profile_db.save_profile(session, user.id, facts, stoplist)
    await session.commit()
    return RedirectResponse("/me/profile", status_code=303)


@router.get("/me", response_class=HTMLResponse)
async def me_index(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    counts = {name: await _count(session, model, user.id) for name, model in TABLES.items()}
    hero = await _latest_ring(session, user.id)
    return templates.TemplateResponse(
        request, "index.html",
        {"counts": counts, "user": user, "hero": hero,
         "base": "/me", "title": "Мої дані", "token": ""},
    )


@router.get("/me/{table}", response_class=HTMLResponse)
async def me_table(
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    type: str = Query(""),
    sort: str = Query("date_desc"),
    days: int = Query(0, ge=0),
    date_from: str = Query(""),
    date_to: str = Query(""),
    resynced: int = Query(-1),          # ST-15: days written by a just-run range resync
    of: int = Query(0),                 # ST-15: days requested in that resync
    resync_error: str = Query(""),      # ST-15: "format" | "range"
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    # Dedicated card views for the user-facing tables.
    if table == "activities":
        # date_from/date_to take priority over days shortcut
        effective_days = 0 if (date_from or date_to) else days
        cards = await _activity_cards(session, user.id, limit, offset,
                                      type_filter=type, days_filter=effective_days, sort=sort,
                                      date_from=date_from, date_to=date_to)
        total = await _activity_count_filtered(session, user.id, type_filter=type,
                                               days_filter=effective_days,
                                               date_from=date_from, date_to=date_to)
        type_counts = await _activity_type_counts(session, user.id,
    days_filter=effective_days, date_from=date_from, date_to=date_to)
        valid_sorts = {k for k, _ in _SORT_OPTIONS}
        safe_sort = sort if sort in valid_sorts else "date_desc"
        filter_label, filters_active = _filter_summary(
            type_counts, type_filter=type, days_filter=effective_days, sort=safe_sort,
            date_from=date_from, date_to=date_to,
        )
        return templates.TemplateResponse(
            request, "activities.html",
            {"acts": cards, "user": user, "tables": list(TABLES), "base": "/me",
             "token": "", "limit": limit, "offset": offset, "total": total,
             "type_filter": type, "days_filter": effective_days, "sort": safe_sort,
             "date_from": date_from, "date_to": date_to,
             "type_counts": type_counts, "sort_options": _SORT_OPTIONS,
             "filter_label": filter_label, "filters_active": filters_active},
        )
    if table == "daily_metrics":
        days = await _daily_cards(session, user.id, limit, offset)
        charts, first_date, last_date = await _daily_trends(session, user.id)
        total = await _count(session, model, user.id)
        hero = _recovery_ring(days[0]) if offset == 0 and days else None
        resync_banner = None
        if resync_error:
            resync_banner = {"ok": False, "error": resync_error}
        elif resynced >= 0:
            resync_banner = {"ok": True, "written": resynced, "requested": of}
        return templates.TemplateResponse(
            request, "daily.html",
            {"days": days, "charts": charts, "first_date": first_date, "last_date": last_date,
             "hero": hero, "user": user, "tables": list(TABLES), "base": "/me", "token": "",
             "limit": limit, "offset": offset, "total": total,
             "resync_banner": resync_banner},
        )
    if table == "report_logs":
        reports = await _report_cards(session, user.id, limit, offset)
        total = await _count(session, model, user.id)
        return templates.TemplateResponse(
            request, "reports.html",
            {"reports": reports, "user": user, "tables": list(TABLES), "base": "/me",
             "token": "", "limit": limit, "offset": offset, "total": total},
        )

    cols = INDEX_COLS.get(table) or [c.name for c in model.__table__.columns]
    table_cols = model.__table__.columns
    pk = list(model.__table__.primary_key.columns)[0]
    order_col = next(
        (table_cols[c] for c in ("date", "created_at") if c in table_cols), pk
    )
    result = await session.execute(
        select(model)
        .where(model.user_id == user.id)
        .order_by(order_col.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [[getattr(r, c) for c in cols] for r in result.scalars().all()]
    total = await _count(session, model, user.id)

    # (activities / daily_metrics / report_logs have dedicated views above; this generic
    # table path remains as a safe fallback for any future table.)
    return templates.TemplateResponse(
        request, "table.html",
        {
            "table": table, "cols": cols, "rows": rows, "user": user,
            "limit": limit, "offset": offset, "total": total,
            "tables": list(TABLES), "base": "/me", "token": "",
            "charts": None, "first_date": None, "last_date": None,
        },
    )


# UI-07: the activity page's outcome notices, as data. Every one of these used to be a
# hand-styled `<div class="note" style="border-left:3px solid #9ece6a">` in the template.
_REGEN_BANNERS = {
    "ok": ("ok", "🔁", "Розбір перегенеровано."),
    "err": ("danger", "⚠️", "Не вдалося перегенерувати — попередній розбір збережено."),
    "nokey": ("danger", "🔑", "Додай Claude-ключ, щоб генерувати розбір."),
    "wait": ("warn", "⏳", "Зачекай хвилину перед повторною перегенерацією."),
    "demo": ("danger", "🎭", "Демо-акаунт: перегенерація вимкнена."),
}


# UI-08: the labels the step bar reads. The kind is the plan's own vocabulary.
_STEP_KIND_LABELS = {"run": "відрізок", "tempo": "темповий", "interval": "інтервал"}


def _stepbar_block(step_match) -> dict | None:
    """UI-08: NF-14's per-step verdict as rows, not as "🎯 7/8 у цілі".

    8×400 with the last two blown is a different session from an even shortfall on all
    eight — one says endurance ran out, the other says the target pace was wrong — and
    the counter renders them identically. ``steps`` is the additive field
    ``stepmatch.match`` now returns; a row stored before that keeps rendering the badge
    alone, which is why this returns ``None`` rather than inventing anything.
    """
    if not isinstance(step_match, dict):
        return None
    steps = step_match.get("steps")
    if not steps:
        return None
    # The widest miss sets the scale, so the bars are comparable within one session.
    worst = max((abs(s["delta_s"]) for s in steps
                 if isinstance(s.get("delta_s"), (int, float))), default=0)
    rows = []
    for i, s in enumerate(steps, start=1):
        delta = s.get("delta_s")
        rows.append({
            "n": i,
            "label": _STEP_KIND_LABELS.get(s.get("kind"), s.get("kind") or "крок"),
            "planned": s.get("planned"),
            "actual": s.get("actual"),
            "hit": s.get("hit"),
            "delta_s": delta,
            # Not run at all: the module already treats it as an honest miss, and the UI
            # must say "не виконано" rather than draw a 0:00 that never happened.
            "missing": s.get("actual") is None,
            "width_pct": (round(100 * abs(delta) / worst) if worst and delta else 0),
            "slower": bool(delta and delta > 0),
        })
    return {"rows": rows, "total": len(rows),
            "hit": sum(1 for r in rows if r["hit"])}


async def _strength_block(session, user_id: int, obj) -> dict | None:
    """UI-06: this session's tonnage and per-exercise e1RM, with the change against the
    previous time each lift was trained.

    All from ``app.strengthstats`` over rows already in the DB — no Garmin request, no
    formula in the router. ``None`` for anything that isn't a strength session with
    stored sets, so the block simply doesn't render."""
    from app import strengthstats

    if not isinstance(obj.exercises, dict) or not obj.exercises:
        return None
    tonnage = strengthstats.session_tonnage(obj.exercises)
    e1rm = strengthstats.session_e1rm(obj.exercises)
    if not tonnage and not e1rm:
        return None

    # The most recent earlier session that trained each lift — "did it go up since last
    # time" is the question a strength page exists to answer.
    previous: dict = {}
    for row in await repository.strength_sessions(session, user_id, weeks=52):
        if not row.get("date") or row["date"] >= (obj.date or ""):
            continue
        for name, value in strengthstats.session_e1rm(row.get("exercises")).items():
            previous[name] = value      # rows come oldest-first, so the last wins

    lifts = []
    for name in sorted(set(tonnage) | set(e1rm)):
        prev = previous.get(name)
        cur = e1rm.get(name)
        lifts.append({
            "name": name,
            "tonnage_kg": tonnage.get(name),
            "e1rm": cur,
            "prev_e1rm": prev,
            "delta": (round(cur - prev, 1) if cur is not None and prev is not None
                      else None),
        })
    return {
        "total_tonnage_kg": round(sum(tonnage.values()), 1) if tonnage else None,
        "total_reps": sum(strengthstats.session_reps(obj.exercises).values()) or None,
        "lifts": lifts,
    }


def _debrief_block(obj) -> dict | None:
    """UI-05: NF-23's per-km breakdown of a session, shown as a curve and two numbers
    instead of a paragraph in Telegram.

    Built from the STORED series only — no splits fetch, so opening an activity page
    still costs zero Garmin requests. ``build_debrief`` degrades honestly: a session
    without enough kilometres yields no curve, and then there is nothing to show.
    """
    from app import postrace

    d = postrace.build_debrief(
        series=obj.series, dist_km=obj.dist_km, dur_min=obj.dur_min, avg_hr=obj.avg_hr)
    curve = d.get("km_curve")
    if not curve:
        return None
    return {
        "curve": curve,
        "halves": d.get("halves"),
        "fade_km": d.get("fade_km"),
        "decoupling_pct": d.get("decoupling_pct"),
        "avg_pace": d.get("avg_pace_min_km"),
        "avg_gap_pace": d.get("avg_gap_pace_min_km"),
        # The same pace sparkline primitive the charts above use — one km per point.
        "series": _trend_series([r.get("pace_min_km") for r in curve],
                                [f"{r.get('km')} км" for r in curve]),
    }


_CHECKIN_BANNERS = {
    "ok": ("ok", "✅", "Записав."),
    "bad": ("warn", "🤔", "Не зрозумів оцінку — спробуй ще раз."),
    "demo": ("danger", "🎭", "Демо-акаунт: чекін вимкнено."),
}


def _activity_banners(*, resynced: bool, regen: str, hidden: bool, shown: bool,
                      is_hidden: bool, checkin: str = "") -> list:
    out = []
    if resynced:
        out.append(banner("ok", "Дані активності оновлено з Garmin.", icon="🔄"))
    if checkin in _CHECKIN_BANNERS:
        level, icon, text = _CHECKIN_BANNERS[checkin]
        out.append(banner(level, text, icon=icon))
    if regen in _REGEN_BANNERS:
        level, icon, text = _REGEN_BANNERS[regen]
        link = "/settings" if regen == "nokey" else ""
        out.append(banner(level, text, icon=icon, link=link,
                          link_text="Налаштування →" if link else ""))
    if hidden:
        out.append(banner(
            "warn",
            "Активність приховано — вона зникла з усіх списків, рекордів і матчингу.",
            icon="🙈"))
    if shown:
        out.append(banner("ok", "Активність знову видима.", icon="👁"))
    # The standing state, as opposed to the "you just did this" note above it.
    if is_hidden and not hidden:
        out.append(banner("muted", "Ця активність прихована.", icon="🙈"))
    return out


@router.get("/me/{table}/{row_id}", response_class=HTMLResponse)
async def me_row(
    table: str,
    row_id: int,
    request: Request,
    resynced: int = Query(0),           # ST-15: 1 right after a successful activity resync
    regen: str = Query(""),             # ST-19: ok|err|nokey|wait after a regenerate attempt
    hidden: int = Query(0),             # ST-17: 1 right after hiding this activity
    shown: int = Query(0),              # ST-17: 1 right after un-hiding it
    checkin: str = Query(""),           # UI-04: ok|bad|demo after a web check-in
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    pk = list(model.__table__.primary_key.columns)[0]
    obj = (
        await session.execute(
            select(model).where(pk == row_id, model.user_id == user.id)
        )
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="Row not found")  # not yours / missing

    # Activities get a dedicated hero + stats + charts view.
    if table == "activities":
        emoji, color = _act_meta(obj.type)
        runwalk = (obj.type or "").lower() in _RUNWALK
        gear_name = None
        if obj.gear_id:
            from app import gear as gear_mod
            roster_json = await repository.get_state(session, user.id, gear_mod.STATE_KEY)
            try:
                roster = json.loads(roster_json) if roster_json else []
            except (ValueError, TypeError):
                roster = []
            gear_name = gear_mod.name_for(obj.gear_id, roster)
        a = {
            "id": obj.id, "emoji": emoji, "color": color,
            "label": act_label(obj.type) or "—",
            "date": _nice_date(obj.date),
            "dist_km": obj.dist_km, "dur_min": obj.dur_min,
            "avg_hr": obj.avg_hr, "max_hr": obj.max_hr, "load": obj.load,
            "pace": _pace_str(obj.dist_km, obj.dur_min) if runwalk else None,
            "exercises": obj.exercises,
            "exercise_rows": _exercise_rows(obj.exercises),
            "rpe": (obj.subjective or {}).get("rpe"),
            "pain": (obj.subjective or {}).get("note") or (obj.subjective or {}).get("pain"),
            "step_badge": stepmatch.badge(obj.step_match),
            "is_hidden": bool(obj.is_hidden),
            "gear_name": gear_name,
        }
        # NF-25/NF-33: form drift and the same-route comparison, both deterministic lines
        # (no LLM). Absent for a watch without dynamics / a run with no recognised route.
        from app import routes as routes_mod
        from app import rundynamics
        from app.garmin.repository import routes as routes_repo

        a["dynamics_line"] = rundynamics.summary(
            rundynamics.session_dynamics(obj.series, dur_min=obj.dur_min))
        route_ctx = await routes_repo.build_route_context(session, user.id, obj)
        route_obj = await routes_repo.get_route(session, user.id, obj.route_id) \
            if obj.route_id else None
        a["route_id"] = obj.route_id
        a["route_name"] = route_obj.name if route_obj else None
        a["route_line"] = routes_mod.summary(route_ctx, a["route_name"])
        strain = None
        if obj.load:
            strain = {"value": int(obj.load), "color": "#3aa0ff", "label": "Навантаження",
                      **_ring_geom(obj.load / 2, 76)}   # load ~0..200 → 0..100%
        charts, first_x, last_x = _run_charts(obj.series or [])
        stepbar = _stepbar_block(obj.step_match)
        # UI-08: shade the scored intervals on the pace curve, so "7/8" is readable off
        # the line itself rather than only as a number next to it.
        if stepbar and charts:
            zones = _shade_zones(obj.series or [], (obj.step_match or {}).get("steps") or [])
            for c in charts:
                if c.get("fmt") == "pace":
                    c["zones"] = zones
        debrief = _debrief_block(obj)
        strength = await _strength_block(session, user.id, obj)
        return templates.TemplateResponse(
            request, "activity.html",
            {"a": a, "strain": strain, "charts": charts, "first_x": first_x, "last_x": last_x,
             "analysis": obj.analysis, "user": user, "base": "/me", "token": "",
             "banners": _activity_banners(
                 resynced=bool(resynced), regen=regen, hidden=bool(hidden),
                 shown=bool(shown), is_hidden=bool(obj.is_hidden), checkin=checkin),
             "pain_parts": subjective.PAIN_PARTS,
             "debrief": debrief, "strength": strength, "stepbar": stepbar,
             "has_claude_key": bool(user.anthropic_key_enc)},
        )

    if table == "report_logs":
        label, color = _kind_meta(obj.kind)
        return templates.TemplateResponse(
            request, "report.html",
            {"r": obj, "label": label, "color": color,
             "when": obj.created_at.strftime("%d.%m.%Y %H:%M") if obj.created_at else "",
             "user": user, "base": "/me", "token": ""},
        )

    if table == "daily_metrics":
        from app import completeness
        ex = obj.extra or {}
        expected = completeness.expected_fields(
            await repository.read_history(session, user.id, days=30)
        )
        incomplete = completeness.labels(completeness.daily_completeness(obj, expected))
        prior = await _prior_days(session, user.id, obj.date)
        return templates.TemplateResponse(
            request, "day.html",
            {"m": obj, "date": _nice_date(obj.date), "hrv_color": _hrv_color(obj.hrv_status),
             "extra": ex, "d": _day_view(obj, ex, prior), "incomplete": incomplete,
             "user": user, "base": "/me", "token": ""},
        )

    fields = [(c.name, getattr(obj, c.name))
              for c in model.__table__.columns if c.name not in ("series", "analysis")]
    charts, first_x, last_x = _run_charts(getattr(obj, "series", None) or [])
    return templates.TemplateResponse(
        request, "detail.html",
        {"table": table, "fields": fields, "user": user, "base": "/me", "token": "",
         "charts": charts, "first_x": first_x, "last_x": last_x,
         "analysis": getattr(obj, "analysis", None)},
    )
