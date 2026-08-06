"""UI-06: ``GET /strength`` — the strength half of the plan, finally charted.

``app.strengthstats`` (NF-27) has computed session tonnage, Epley e1RM, weekly stats,
trends and stalls since it shipped; a grep across the routers and templates found zero
references. All of it existed only as prompt context and one line in Telegram.

The asymmetry that made this worth fixing: running gets charts, efficiency trends,
records and period comparisons, while strength — an equal half of the plan, with its own
``type="strength"`` sessions and its own intake questions — was a flat list of exercise
names. So the most basic strength question, "has my squat gone up in three months?",
had no answer, although the answer was already computed and thrown away.

The page computes nothing: every number comes from ``app.strengthstats``. Zero Claude
calls, zero Garmin requests — the sets were stored when the activity was synced.
"""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import strengthstats
from app.charts import bar_series, trend_series
from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.templating import create_templates

templates = create_templates()
router = APIRouter(tags=["strength"])

# Long enough to show a real e1RM trend and a stall, short enough not to drag in last
# season's block. Same window the digest's strength context uses.
WEEKS = 26
# What "recent" means for the exercise picker — the lifts currently in rotation.
RECENT_WEEKS = 4


def _exercise_block(weeks: list, exercise: str) -> dict:
    """One lift's e1RM history. A single data point is shown as a value, never as a
    "trend": two points do not make a line, and one point does not even make two."""
    points = [(w["week"], w["by_exercise"][exercise]["e1rm"])
              for w in weeks
              if exercise in w["by_exercise"] and w["by_exercise"][exercise]["e1rm"]]
    trend = strengthstats.e1rm_trend(weeks, exercise)
    return {
        "exercise": exercise,
        "points": points,
        "current": points[-1][1] if points else None,
        "trend": trend,
        # None below two points — the template then shows the number alone.
        "series": (trend_series([p[1] for p in points], [p[0] for p in points])
                   if len(points) >= 2 else None),
        "first_week": points[0][0] if points else "",
        "last_week": points[-1][0] if points else "",
    }


@router.get("/strength", response_class=HTMLResponse)
async def strength(
    request: Request,
    exercise: str = Query(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await repository.strength_sessions(session, user.id, weeks=WEEKS)
    weeks = strengthstats.weekly_stats(rows)
    if not weeks:
        return templates.TemplateResponse(
            request, "strength.html",
            {"user": user, "weeks": [], "empty": True},
        )

    # The lifts currently in rotation, heaviest first — the picker, and the default.
    recent = strengthstats.recent_lifts(rows, weeks=RECENT_WEEKS)
    lifts = sorted(recent, key=lambda n: -(recent[n].get("e1rm") or 0))
    if not lifts:
        lifts = sorted({n for w in weeks for n in w["by_exercise"]})
    chosen = exercise if exercise in lifts else (lifts[0] if lifts else "")

    tonnage = bar_series([w["tonnage_kg"] for w in weeks], [w["week"] for w in weeks])

    return templates.TemplateResponse(
        request, "strength.html",
        {
            "user": user,
            "empty": False,
            "weeks": weeks,
            "lifts": lifts,
            "chosen": chosen,
            "block": _exercise_block(weeks, chosen) if chosen else None,
            "tonnage": tonnage,
            "last_week": weeks[-1],
            "stalls": strengthstats.detect_stalls(weeks),
            "recent": recent,
            # Shown next to every e1RM so an ESTIMATE never reads as a measurement.
            "e1rm_note": {
                "max_reps": strengthstats.E1RM_MAX_REPS,
                "top_sets": strengthstats.TOP_SETS,
                "warmup_pct": int(strengthstats.WARMUP_FRACTION * 100),
                "stall_weeks": strengthstats.STALL_WEEKS,
            },
            "window_weeks": WEEKS,
        },
    )
