"""Per-user data view — a logged-in user browses their own metrics, activities and
reports (scoped to their user_id). Mirrors the admin /ui browser but never spans
other users, and excludes the users / bot_state tables."""
import csv
import datetime as dt
import io
import json
import math
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, nullslast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import format as fmt
from app import stepmatch
from app.charts import run_charts as _run_charts
from app.charts import trend_series as _trend_series
from app.core.auth import current_user
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

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
    stmt = select(ActivityRecord).where(ActivityRecord.user_id == user_id)
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
        t = (r.type or "").lower()
        label = _TYPE_LABELS.get(t) or t.replace("_", " ").capitalize()
        cards.append({
            "id": r.id, "emoji": emoji, "color": color, "label": label,
            "date": _nice_date(r.date),
            "dist_km": r.dist_km, "dur_min": r.dur_min,
            "avg_hr": r.avg_hr, "max_hr": r.max_hr, "load": r.load,
            "pace": _pace_str(r.dist_km, r.dur_min) if runwalk else None,
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
            .where(ActivityRecord.user_id == user_id))
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
        .where(ActivityRecord.user_id == user_id)
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
        {"type": t, "count": n,
         "emoji": _act_meta(t)[0],
         "label": _TYPE_LABELS.get((t or "").lower()) or (t or "").replace("_", " ").capitalize()}
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


_SKIP_KEYS = frozenset({
    "resting_hr", "readiness_score", "auto_activities",
    "sleep_feedback", "hrs_feedback", "hrv_feedback",
    "readiness_feedback", "acwr_feedback", "endurance_class",
    "hrv_5day_high",
})

_SECTIONS_DEF = [
    ("Сон", [
        ("overnight_hrv",    "HRV нічний",       None),
        ("avg_hr_sleep",     "пульс уві сні",    None),
        ("awake_count",      "пробуджень",        None),
        ("restless_moments", "неспок. моменти",   None),
        ("sleep_need_h",     "потреба, год",      None),
        ("skin_temp_dev_c",  "темп. шкіри °C",   None),
        ("spo2_avg",         "SpO₂ %",            None),
        ("respiration_avg",  "дихання / хв",      None),
        ("body_battery_change", "BB зміна",       None),
    ]),
    ("HRV (деталі)", [
        ("hrv_weekly_avg",   "тижн. серед.",      None),
        ("hrv_5min_high",    "5хв макс",          None),
        ("hrv_baseline_low", "норма від",         None),
        ("hrv_baseline_high","норма до",          None),
    ]),
    ("Тренувальне навантаження", [
        ("recovery_time_h",  "відновлення, год",  None),
        ("acute_load",       "гостре навант.",    None),
        ("acwr_pct",         "ACWR %",            None),
    ]),
    ("Активність дня", [
        ("steps",            "кроки",             None),
        ("distance_m",       "відстань",          _fmt_dist),
        ("calories",         "ккал",              None),
        ("moderate_min",     "помірна, хв",       None),
        ("active_min",       "активні, хв",       None),
        ("floors",           "поверхи",           None),
        ("min_hr",           "мін. пульс",        None),
        ("bb_high",          "BB макс",           None),
        ("bb_low",           "BB мін",            None),
    ]),
    ("Прогнози", [
        ("race_5k_s",        "5К",                _fmt_race),
        ("race_10k_s",       "10К",               _fmt_race),
        ("race_half_s",      "напівмарафон",      _fmt_race),
        ("race_marathon_s",  "марафон",           _fmt_race),
        ("vo2max",           "VO₂max",            None),
        ("fitness_age",      "фітнес-вік",        None),
        ("endurance_score",  "витривалість",      None),
    ]),
]


def _day_sections(ex: dict) -> list:
    known = {k for _, fields in _SECTIONS_DEF for k, *_ in fields} | _SKIP_KEYS
    out = []
    for title, fields in _SECTIONS_DEF:
        items = [
            {"label": lbl, "value": fmt(ex[k]) if fmt else ex[k]}
            for k, lbl, fmt in fields if ex.get(k) is not None
        ]
        if items:
            out.append({"title": title, "rows": items})
    leftovers = [
        {"label": k.replace("_", " "), "value": v}
        for k, v in ex.items()
        if k not in known and v is not None and isinstance(v, (int, float, str))
    ]
    if leftovers:
        out.append({"title": "Інше", "rows": leftovers})
    return out


async def _daily_cards(session, user_id, limit, offset):
    rows = (await session.execute(
        select(DailyMetric).where(DailyMetric.user_id == user_id)
        .order_by(DailyMetric.date.desc()).limit(limit).offset(offset)
    )).scalars().all()
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
    "exercises", "series", "analysis", "subjective", "step_match", "created_at",
]
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
    buf.seek(0)

    fname = f"garmin-coach-export-{dt.date.today().isoformat()}.zip"
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
        return templates.TemplateResponse(
            request, "activities.html",
            {"acts": cards, "user": user, "tables": list(TABLES), "base": "/me",
             "token": "", "limit": limit, "offset": offset, "total": total,
             "type_filter": type, "days_filter": effective_days, "sort": safe_sort,
             "date_from": date_from, "date_to": date_to,
             "type_counts": type_counts, "sort_options": _SORT_OPTIONS},
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


@router.get("/me/{table}/{row_id}", response_class=HTMLResponse)
async def me_row(
    table: str,
    row_id: int,
    request: Request,
    resynced: int = Query(0),           # ST-15: 1 right after a successful activity resync
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
        a = {
            "id": obj.id, "emoji": emoji, "color": color,
            "label": (obj.type or "—").replace("_", " ").capitalize(),
            "date": _nice_date(obj.date),
            "dist_km": obj.dist_km, "dur_min": obj.dur_min,
            "avg_hr": obj.avg_hr, "max_hr": obj.max_hr, "load": obj.load,
            "pace": _pace_str(obj.dist_km, obj.dur_min) if runwalk else None,
            "exercises": obj.exercises,
            "rpe": (obj.subjective or {}).get("rpe"),
            "pain": (obj.subjective or {}).get("note") or (obj.subjective or {}).get("pain"),
            "step_badge": stepmatch.badge(obj.step_match),
        }
        strain = None
        if obj.load:
            strain = {"value": int(obj.load), "color": "#3aa0ff", "label": "Навантаження",
                      **_ring_geom(obj.load / 2, 76)}   # load ~0..200 → 0..100%
        charts, first_x, last_x = _run_charts(obj.series or [])
        return templates.TemplateResponse(
            request, "activity.html",
            {"a": a, "strain": strain, "charts": charts, "first_x": first_x, "last_x": last_x,
             "analysis": obj.analysis, "user": user, "base": "/me", "token": "",
             "resynced": bool(resynced)},
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
        ex = obj.extra or {}
        return templates.TemplateResponse(
            request, "day.html",
            {"m": obj, "date": _nice_date(obj.date), "hrv_color": _hrv_color(obj.hrv_status),
             "extra": ex, "sections": _day_sections(ex), "user": user, "base": "/me", "token": ""},
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
