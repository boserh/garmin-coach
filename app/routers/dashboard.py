"""EP-04: the mobile-first web dashboard — a single logged-in-user overview page
(readiness today, 30-day recovery trends, next 7 days of the active plan, last 5
activities, this month's AI cost) instead of paging through the raw /me tables.

Pure DB reads only — no Garmin/Claude call on this path, so it renders fast and free.
Reuses the same building blocks as /me and /plan (the hero ring, trend charts, plan
row markup, activity cards) rather than growing a parallel chart/markup stack.
"""
import datetime as dt
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import baselines, onboarding
from app.banners import banner
from app.charts import trend_series as _trend_series
from app.core.auth import current_user
from app.core.config import settings
from app.core.tz import user_today
from app.db import lifestyle as lifestyle_db
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository, service
from app.routers.me import _act_meta, _nice_date, _ring_geom, act_label, act_pace
from app.routers.plan import _dm, _dow, fueling_today
from app.templating import create_templates

templates = create_templates()
templates.env.filters["dow"] = _dow
templates.env.filters["dm"] = _dm

router = APIRouter(tags=["dashboard"])

TREND_DAYS = 30
PLAN_WINDOW_DAYS = 7
ACTIVITIES_N = 5

# label/colour/format for each 30-day trend sparkline, sourced from repository.read_history
_TREND_DEFS = [
    ("HRV", "#7aa2f7", "int", "hrv_avg"),
    ("Пульс спокою", "#f7768e", "int", "resting_hr"),
    ("Сон, год", "#9ece6a", "f1", "sleep_h"),
    ("Стрес", "#e0af68", "int", "stress_avg"),
]


def _trend_charts(trend: list) -> tuple:
    """30-day HRV/RHR/sleep/stress sparklines with hover — same shape as the /me
    daily view's charts, just a different metric set (adds RHR + stress)."""
    dates = [r["date"] for r in trend]
    charts = [
        {"label": lbl, "color": c, "fmt": fmt, "s": s}
        for lbl, c, fmt, key in _TREND_DEFS
        if (s := _trend_series([r.get(key) for r in trend], dates))
    ]
    return charts, (dates[0] if dates else ""), (dates[-1] if dates else "")


def _activity_cards(rows: list) -> list:
    """The same card /me/activities builds, through the same helpers.

    It used to be a second, worse copy: it printed the raw Garmin slug as the title
    (`stand_up_paddleboarding_v2`), the ISO date, and — because it applied
    distance ÷ duration unconditionally — a pace under sessions that don't have one
    ("22:52 /км" on a paddleboard)."""
    out = []
    for a in rows:
        emoji, color = _act_meta(a["type"])
        out.append({
            "id": a["id"], "date": _nice_date(a["date"]),
            # The ISO date stays alongside the formatted one: _checkin_prompt compares it
            # against a cutoff, and "Ср, 5 серпня 2026" > "2026-08-05" is not a date test.
            "date_iso": a["date"],
            "type": act_label(a["type"]),
            "emoji": emoji, "color": color,
            "dist_km": a["dist_km"], "dur_min": a["dur_min"], "avg_hr": a["avg_hr"],
            "load": a["load"], "rpe": a["rpe"], "has_checkin": a.get("has_checkin"),
            "pace": act_pace(a["type"], a["dist_km"], a["dur_min"]),
        })
    return out


# UI-04: how fresh a session has to be before we ask how it went. Asking about every
# un-rated run forever is nagging; asking about the one you just did is the feature.
CHECKIN_PROMPT_DAYS = 2


def _checkin_prompt(cards: list, today: dt.date) -> dict | None:
    """The newest activity worth asking about — the latest one from today or yesterday
    with no check-in at all — or ``None``, which is most days."""
    cutoff = (today - dt.timedelta(days=CHECKIN_PROMPT_DAYS - 1)).isoformat()
    for a in cards:
        # `date_iso`, never the display date — the latter is "Ср, 5 серпня 2026".
        if (a.get("date_iso") or a["date"]) >= cutoff and not a.get("has_checkin"):
            return a
    return None


# EP-17: multi-ring hero (Bevel-style Strain/Recovery/Sleep) — replaces the single
# _hero_ring.html on /dashboard only; _hero_ring.html/_recovery_ring stay in app/routers/me.py
# unchanged for /me, which doesn't get this upgrade yet.
_RING_R_SM = 34
_LOAD_COLOR = "#e0af68"      # --tempo
_RECOVERY_COLOR = "#73daca"  # --recovery
_SLEEP_COLOR = "#7dcfff"     # --long


def _load_ring(extra: dict) -> dict:
    """Навантаження ring: today's ACWR% straight from Garmin's readiness endpoint — not
    the RICE 'strain' Bevel shows (see EP-17 pitfalls), an honest ACWR% instead. Empty
    until Garmin has enough load history to compute it."""
    acwr = (extra or {}).get("acwr_pct")
    if acwr is None:
        return {"empty": True, "label": "Навантаження", "color": _LOAD_COLOR}
    return {
        "empty": False, "label": "Навантаження", "color": _LOAD_COLOR,
        "value": round(acwr), "unit": "%", **_ring_geom(acwr, _RING_R_SM),
    }


def _recovery_ring(norm: Optional[dict]) -> dict:
    """Recovery ring: today's HRV mapped onto its NF-01 personal band (p25..p75 around
    p50) — a position within your own normal, not a raw HRV number pretending to be a
    percentage. p50 lands at 50%, p25/p75 at 15%/85%, clamped beyond that."""
    hrv = ((norm or {}).get("metrics") or {}).get("hrv_avg")
    if not hrv:
        return {"empty": True, "label": "Відновлення", "color": _RECOVERY_COLOR}
    lo, hi = hrv["band"]
    frac = 50.0 if hi <= lo else 15 + 70 * (hrv["cur"] - lo) / (hi - lo)
    return {
        "empty": False, "label": "Відновлення", "color": _RECOVERY_COLOR,
        "value": int(round(hrv["cur"])), "unit": "", **_ring_geom(frac, _RING_R_SM),
    }


def _sleep_ring(day: dict) -> dict:
    """Sleep ring: today's Garmin sleep score — already 0-100, no mapping needed."""
    score = (day or {}).get("sleep_score")
    if score is None:
        return {"empty": True, "label": "Сон", "color": _SLEEP_COLOR}
    return {
        "empty": False, "label": "Сон", "color": _SLEEP_COLOR,
        "value": int(score), "unit": "%", **_ring_geom(score, _RING_R_SM),
    }


def _stat_cards(day: dict) -> list:
    """Compact HRV/RHR/Body Battery/Stress tiles for the stat-grid below the rings —
    replaces the old `.hs` line list. A metric this user doesn't have that day is
    omitted, not zero-filled. Stress shows avg/max only: Garmin's dailyStress DTO has no
    documented minimum field, so a "Lowest" number would be fabricated (EP-17 note)."""
    extra = (day or {}).get("extra") or {}
    cards = []
    if day.get("hrv_avg") is not None:
        cards.append({"label": "HRV", "value": day["hrv_avg"], "sub": None})
    if extra.get("resting_hr") is not None:
        cards.append({"label": "Пульс спокою", "value": extra["resting_hr"], "sub": None})
    if day.get("bb_charged") is not None:
        cards.append({"label": "Body Battery", "value": day["bb_charged"], "sub": None})
    if day.get("stress_avg") is not None:
        sub = f"макс {day['stress_max']}" if day.get("stress_max") is not None else None
        cards.append({"label": "Стрес", "value": day["stress_avg"], "sub": sub})
    return cards


def _setup_missing(user) -> list:
    """Short names of the setup steps this account still owes, from the one place that
    decides what "done" means (app.onboarding). The optional plan step is never in it,
    so no plan query is needed here — see onboarding.missing_labels."""
    if user.is_demo:
        return []   # the demo account has nothing to configure and no way to do it
    return onboarding.missing_labels(onboarding.build_steps(
        has_garmin=user.has_garmin_setup,
        garmin_invalid=user.garmin_creds_invalid,
        has_anthropic=bool(user.anthropic_key_enc),
        has_telegram=user.telegram_chat_id is not None,
    ))


def _dashboard_banners(user, *, garmin_errors, backup, backup_warn_days, llm_budget,
                       has_history, setup_missing=()) -> list:
    """The page's notices, as data (UI-07) — colour and ARIA role come from the level,
    never from a hex typed into the template."""
    out = []

    # First, because nothing else on this page can work until it's done. An account that
    # never finished setup used to see only "ще немає історії" — true, and no help at all.
    # Suppressed when a rejected Garmin password is the ONLY thing outstanding: the
    # dedicated banner right below says the same thing with a better link.
    if setup_missing and not (user.garmin_creds_invalid and len(setup_missing) == 1):
        out.append(banner(
            "warn",
            "Налаштування не завершено: лишилось підключити "
            + ", ".join(setup_missing) + ". До цього дані не тягнуться і звітів не буде.",
            icon="🔌", link="/onboarding", link_text="Закінчити підключення →"))

    if user.garmin_creds_invalid:
        out.append(banner(
            "danger", "Garmin не приймає збережені email/пароль — синк зупинено.",
            icon="🔑", link="/settings", link_text="Оновити креденшели →"))

    if garmin_errors and garmin_errors.get("count_24h"):
        counts = garmin_errors.get("counts_24h") or {}
        detail = ", ".join(f"{k}×{n}" for k, n in counts.items())
        out.append(banner(
            "warn",
            f"Garmin API: {garmin_errors['count_24h']} збоїв за 24 год"
            + (f" ({detail})" if detail else "")
            + ". Можливо, тимчасова деградація неофіційного API.",
            icon="⚠️", link="/status", link_text="Подивитись статус →"))

    # OPS-08: a missing marker means backups were never configured; a stale one means
    # they stopped. Both matter, and they read differently.
    if backup and (backup.get("age_hours") is None
                   or backup["age_hours"] >= backup_warn_days * 24
                   or backup.get("rsync_ok") is False):
        if backup.get("age_hours") is None:
            text = "Бекап БД: маркер не знайдено — схоже, бекапи ще не налаштовані."
        else:
            text = f"Бекап БД: останній {backup['age_hours'] / 24:.1f} дн тому."
        if backup.get("rsync_ok") is False:
            text += " Off-SD копіювання (rsync) останнього разу не вдалось."
        out.append(banner("warn", text, icon="⚠️", link="/status",
                          link_text="Подивитись статус →"))

    if llm_budget and (llm_budget.get("warn") or llm_budget.get("blocked")):
        blocked = llm_budget.get("blocked")
        text = (
            ("Бюджет на Claude вичерпано: " if blocked else "Бюджет на Claude: ")
            + f"${llm_budget['month_usd']:.2f} з ${llm_budget['month_limit']:.2f} за "
              f"місяць ({llm_budget['pct']}%), прогноз "
              f"${llm_budget['projected_month_usd']:.2f}."
        )
        if blocked:
            text += " Нові виклики зупинені до наступного періоду."
        elif llm_budget.get("soft_blocked"):
            text += " Фонові звіти призупинені — команди ще працюють."
        out.append(banner("danger" if blocked else "warn", text,
                          icon="🛑" if blocked else "⚠️",
                          link="/me/report_logs", link_text="Витрати →"))

    # Only once setup is done — before that, "нема історії" is a symptom, and the
    # checklist banner above already names the cause.
    if not has_history and not setup_missing:
        out.append(banner(
            "info",
            "Ще немає історії відновлення — після першого синку Garmin тут з'явиться "
            "сьогоднішня готовність і тренди.",
            icon="🌱", link="/settings", link_text="Перевірити налаштування →"))

    return out


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    lifestyle_saved: str = Query("", alias="lifestyle"),   # UI-04: ok|demo after a POST
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    trend = await repository.read_history(session, user.id, days=TREND_DAYS)
    charts, first_x, last_x = _trend_charts(trend)

    # EP-17: multi-ring hero, built from the latest day in `trend` (already carries
    # sleep_score/extra) + a 90-day baselines window for the recovery ring's HRV band.
    today_row = trend[-1] if trend else None
    rings = None
    stat_cards = []
    if today_row is not None:
        history90 = await repository.read_history(session, user.id, days=baselines.WINDOW_DAYS)
        norm = baselines.compute_baselines(history90)
        rings = {
            "date": _nice_date(today_row["date"]),
            "list": [_load_ring(today_row.get("extra") or {}), _recovery_ring(norm),
                     _sleep_ring(today_row)],
        }
        stat_cards = _stat_cards(today_row)

    plan = await repository.get_active_plan(session, user.id)
    upcoming = []
    load_forecast = None
    fueling = None
    if plan is not None:
        window_end = (dt.date.today() + dt.timedelta(days=PLAN_WINDOW_DAYS)).isoformat()
        upcoming = [
            w for w in await repository.list_workouts(session, plan.id, upcoming_only=True)
            if w.date <= window_end
        ]
        load_forecast = await repository.load_forecast(session, user.id)
        # UI-05: NF-11's water/carbs line for today's key session, from the same
        # memoised forecast /plan already uses (no extra Open-Meteo call on a reload).
        fueling = await fueling_today(
            user, await repository.list_workouts(session, plan.id),
            await repository.typical_run_pace(session, user.id))

    activities = _activity_cards(await repository.list_activities(session, user.id, n=ACTIVITIES_N))
    month_cost = await repository.month_cost(session, user.id)

    # UI-04: the two one-tap inputs the analytics are hungriest for and the web had no
    # way to write — a fresh run's RPE, and tonight's lifestyle tags (NF-28).
    today_local = user_today(user)
    lifestyle_row = await lifestyle_db.get_day(session, user.id, today_local.isoformat())
    lifestyle = {
        "date": today_local.isoformat(),
        "tags": list(lifestyle_row.tags or []) if lifestyle_row else [],
        "labels": [(slug, lifestyle_db.label(slug)) for slug in lifestyle_db.TAG_ORDER],
        "saved": lifestyle_saved == "ok",
    }

    # NF-19: an aerobic-efficiency sparkline (weekly-median EF, GAP-honest) when there's a
    # real trend — the "faster at the same HR?" signal, reusing the shared chart primitive.
    from app import efficiency as eff_mod
    eff_trend = eff_mod.build_trend(await repository.runs_for_efficiency(session, user.id))
    eff_chart = None
    if eff_trend and eff_trend.get("status") == "ok":
        weekly = eff_trend["weekly"]
        eff_chart = {
            "series": _trend_series([w["ef"] for w in weekly], [w["week"] for w in weekly]),
            "pct_change": eff_trend["pct_change"],
            "delta_pace_s": eff_trend["delta_pace_s"],
            "typical_hr": eff_trend["typical_hr"],
            "first_week": weekly[0]["week"], "last_week": weekly[-1]["week"],
        }

    # OPS-05: a banner when the Garmin API threw failures in the last 24h (degradation vs a
    # watch that just hasn't synced). Expected garth 403 gaps are excluded from the count.
    garmin_errors = service.summarize_garmin_errors(
        await repository.get_state(session, user.id, service.GARMIN_ERRORS_KEY)
    )

    # OPS-08: DB backup freshness — admin-only (a per-install fact, not per-user).
    backup = None
    if user.is_admin:
        from app import backup_status
        backup = backup_status.read_status(Path(settings.BACKUP_DIR))

    # OPS-11: the spend breaker's own state, so the ceiling is visible BEFORE it silently
    # starts skipping morning reports. Same DB rows the cost tile above reads.
    from app.analysis import budget as budget_mod
    llm_budget = budget_mod.status(await budget_mod.spend_totals(session, user.id))

    # NF-24: this week's easy/grey/hard split. Absent (not zeroed) for a user whose
    # activities carry no HR zones — the tile simply doesn't render.
    from app.analysis.reports import build_intensity_context
    intensity_ctx = await build_intensity_context(session, user_id=user.id)
    intensity_week = (intensity_ctx.get("weeks") or [None])[-1] if intensity_ctx else None

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "banners": _dashboard_banners(
                user, garmin_errors=garmin_errors, backup=backup,
                backup_warn_days=settings.BACKUP_WARN_DAYS, llm_budget=llm_budget,
                has_history=bool(trend), setup_missing=_setup_missing(user)),
            "checkin_prompt": _checkin_prompt(activities, today_local),
            "fueling": fueling,
            "lifestyle": lifestyle, "lifestyle_back": "/dashboard",
            "user": user, "rings": rings, "stat_cards": stat_cards,
            "charts": charts, "first_x": first_x, "last_x": last_x,
            "has_history": bool(trend),
            "plan": plan, "upcoming": upcoming, "load_forecast": load_forecast,
            "activities": activities,
            "month_cost": month_cost,
            "today_iso": dt.date.today().isoformat(),
            "garmin_errors": garmin_errors,
            "eff_chart": eff_chart,
            "backup": backup,
            "backup_warn_days": settings.BACKUP_WARN_DAYS,
            "llm_budget": llm_budget,
            "intensity_week": intensity_week,
            "intensity_findings": (intensity_ctx or {}).get("findings") or [],
        },
    )
