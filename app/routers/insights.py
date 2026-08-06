"""UI-05: ``GET /insights`` — everything the app already computes, finally visible.

A dozen pure, deterministic modules (``injury``, ``loadforecast``, ``correlations``,
``wrapped``, ``compare``, ``returntorun``) each had a finished API, cost nothing to run
— and no web surface at all. Their output reached the user only as a paragraph of
Telegram text that can't be revisited, compared, or shown to a doctor.

Two hard rules, both covered by tests:

* **This page runs no LLM and makes no Garmin request.** Every number comes out of the
  DB through the same builders the bot uses. "Let Claude phrase it nicer" is a separate,
  paid path (``run_insights``) and deliberately not wired in here.
* **This page computes nothing of its own.** No threshold is re-typed and no number is
  re-derived; the modules already return honest ``None``/``calibrating`` results, and a
  section that has no signal is simply absent rather than rendered empty.
"""
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import compare as compare_mod
from app import correlations, loadforecast, returntorun
from app import wrapped as wrapped_mod
from app.analysis.reports import INSIGHTS_WINDOW_DAYS, build_injury_assessment
from app.charts import trend_series as _trend_series
from app.core.auth import current_user
from app.core.config import settings
from app.core.tz import user_today
from app.db import lifestyle as lifestyle_db
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.templating import create_templates

templates = create_templates()
router = APIRouter(tags=["insights"])

# Comparison spans offered in the UI — inside app.compare's own MIN/MAX bounds.
_COMPARE_SPANS = [4, 12, 26]

# Signals in the order a reader needs them, and what each one is called.
_SIGNAL_LABELS = {
    "acwr": "Навантаження",
    "pain": "Біль",
    "rpe": "Відчуття зусилля",
    "recovery": "Відновлення",
    "intensity": "Розподіл інтенсивності",
    "dynamics": "Форма бігу",
}
_LEVEL_META = {
    "high": ("danger", "🔴", "Підвищений ризик травми"),
    "elevated": ("warn", "🟠", "Кілька сигналів ризику"),
    "none": ("ok", "🟢", "Сигналів ризику немає"),
    "calibrating": ("muted", "⏳", "Калібрування"),
}

# Where each block's numbers come from, so every claim on the page is one tap from its
# evidence — the same transparency rule EP-18's profile follows.
_SOURCES = {
    "risk": ("/me/activities", "тренування та чекіни"),
    "load": ("/plan", "програма"),
    "correlations": ("/me/daily_metrics", "щоденні метрики"),
    "recap": ("/me/activities", "тренування"),
    "compare": ("/me/activities", "тренування"),
    "ladder": ("/plan", "програма"),
}


def _risk_block(assessment) -> dict:
    """The injury radar as cards. ``calibrating`` is a first-class state, not an error:
    the detector stays quiet until there's enough history, and the page says how much is
    still missing rather than inventing a green light."""
    level, icon, head = _LEVEL_META.get(
        assessment.level, _LEVEL_META["none"])
    return {
        "level": level, "icon": icon, "head": head,
        "state": assessment.level,
        "score": assessment.score,
        "history_days": assessment.history_days,
        "min_history_days": settings.INJURY_MIN_HISTORY_DAYS,
        "signals": [
            {"label": _SIGNAL_LABELS.get(s.kind, s.kind), "kind": s.kind,
             "severity": s.severity, "detail": s.detail}
            for s in assessment.signals
        ],
        "source": _SOURCES["risk"],
    }


def _load_block(forecast: dict | None) -> dict | None:
    """NF-20's forward ACWR as a trace instead of a single number: done so far, plus what
    each remaining planned session adds. ``None`` (no plan, or not enough history for an
    honest ACWR) means the section doesn't render at all."""
    if not forecast or forecast.get("acwr") is None:
        return None
    sessions = forecast.get("sessions") or []
    running = forecast.get("done_load") or 0.0
    points, labels = [running], ["зараз"]
    for s in sessions:
        running += s.get("load") or 0.0
        points.append(running)
        labels.append(s.get("date") or "")
    return {
        "acwr": forecast["acwr"],
        "level": forecast.get("level"),
        "done_load": forecast.get("done_load"),
        "load": forecast.get("load"),
        "typical": forecast.get("typical"),
        "delta_pct": forecast.get("delta_pct"),
        "sessions": sessions,
        "series": _trend_series(points, labels),
        "warn_acwr": settings.FORECAST_ACWR_WARN,
        "high_acwr": settings.FORECAST_ACWR_HIGH,
        "source": _SOURCES["load"],
    }


def _correlations_block(findings: list) -> dict | None:
    if not findings:
        return None
    return {
        "findings": findings,
        # The single most misread block on the page — the disclaimer is part of the
        # component, not an optional footnote, and it comes from the module itself.
        "note": correlations.ASSOCIATION_NOTE,
        "min_samples": correlations.MIN_SAMPLES,
        "window_days": INSIGHTS_WINDOW_DAYS,
        "source": _SOURCES["correlations"],
    }


def _ladder_block(state: dict | None) -> dict | None:
    """NF-30's walk/run ladder, when one is actually running."""
    if not returntorun.is_active(state):
        return None
    step = returntorun.step_by_number(state.get("step", 1))
    if step is None:
        return None
    return {
        "step": step, "last_step": returntorun.LAST_STEP,
        "text": returntorun.step_text(state),
        "minutes": returntorun.session_minutes(step),
        "source": _SOURCES["ladder"],
    }


async def _return_state(session, user_id: int) -> dict | None:
    raw = await repository.get_state(session, user_id, returntorun.STATE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@router.get("/insights", response_class=HTMLResponse)
async def insights(
    request: Request,
    period: str = Query("year"),          # NF-07 recap window: year | quarter
    weeks: int = Query(compare_mod.DEFAULT_WEEKS),   # NF-06 comparison span
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    today = user_today(user)

    # ---- what to do now ----
    risk = _risk_block(await build_injury_assessment(session, user_id=user.id))
    ladder = _ladder_block(await _return_state(session, user.id))

    # ---- what's ahead ----
    load = _load_block(await repository.load_forecast(session, user.id, today=today))

    # ---- what the history says ----
    history = await repository.read_history(session, user.id, days=INSIGHTS_WINDOW_DAYS)
    logs = await lifestyle_db.read_range(session, user.id, days=INSIGHTS_WINDOW_DAYS)
    corr = _correlations_block(
        correlations.find_correlations(history, lifestyle_logs=logs))

    period = period if period in wrapped_mod.PERIODS else wrapped_mod.DEFAULT_PERIOD
    r_start, r_end = wrapped_mod.period_window(today, period)
    recap_stats = await repository.wrapped_stats(session, user.id, r_start, r_end)
    recap = None
    if wrapped_mod.has_signal(recap_stats):
        recap = {
            "period": period, "label": wrapped_mod.label(period),
            "range": wrapped_mod.fmt_range(r_start, r_end),
            "stats": recap_stats,
            "records": await repository.records_in_range(session, user.id, r_start, r_end),
            "source": _SOURCES["recap"],
        }

    weeks = max(compare_mod.MIN_WEEKS, min(weeks, compare_mod.MAX_WEEKS))
    c_start, c_end, p_start, p_end = compare_mod.window_pair(today, weeks)
    current = await repository.window_stats(session, user.id, c_start, c_end)
    past = await repository.window_stats(session, user.id, p_start, p_end)
    comparison = None
    if compare_mod.has_signal(current, past):
        comparison = {
            "weeks": weeks, "current": current, "past": past,
            "current_range": compare_mod.fmt_range(c_start, c_end),
            "past_range": compare_mod.fmt_range(p_start, p_end),
            "source": _SOURCES["compare"],
        }

    return templates.TemplateResponse(
        request, "insights.html",
        {
            "user": user,
            "risk": risk, "ladder": ladder, "load": load, "correlations": corr,
            "recap": recap, "comparison": comparison,
            "periods": [(k, wrapped_mod.label(k)) for k in wrapped_mod.PERIODS],
            "compare_spans": _COMPARE_SPANS,
            # Cold-start copy needs real numbers; they come from the modules and the
            # settings, never typed into the template.
            "gates": {
                "injury_days": settings.INJURY_MIN_HISTORY_DAYS,
                "forecast_days": loadforecast.MIN_HISTORY_DAYS,
                "correlation_samples": correlations.MIN_SAMPLES,
                "correlation_window": INSIGHTS_WINDOW_DAYS,
                "history_days": len(history),
            },
            "has_any": any((ladder, load, corr, recap, comparison))
                       or risk["state"] not in ("calibrating",),
        },
    )
