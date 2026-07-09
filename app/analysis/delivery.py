"""Shared report pipeline for the daily-report channels (bot ``/report``, web
``/report.json``, the scheduled morning DM).

The same conveyor — an already-built payload → (weather) → ``run_analysis`` → text +
sync flags — was assembled three times with tiny divergences. ``build_report`` collapses
it to one call; the **channel** owns presentation (a Telegram stale-prefix vs a JSON
``note`` field) and error handling (a bot reply vs an HTTP 502), so this helper stays
free of any Telegram/HTTP knowledge.

Note the payload is passed **in**, not fetched here: the morning tick builds it once and
shares it with the activity-watch, so re-fetching would mean a second Garmin call.
"""
from dataclasses import dataclass
from typing import Optional

from app.analysis.service import run_analysis
from app.db.models import User
from app.garmin.schemas import Payload

# Canonical stale note for the on-demand reports (bot /report + web /report.json). The
# morning job uses its own wording (jobs.py::_MORNING_STALE — "звіт" not "аналіз"): a
# deliberate difference kept there because morning also decides stale via a stricter
# recovery-synced check, not payload.synced_today.
STALE_NOTE = "⚠️ Дані за сьогодні ще не синканулись, аналіз за останній доступний день."


@dataclass
class ReportResult:
    text: str
    synced_today: bool
    last_data_date: Optional[str]


async def build_report(
    session,
    user: User,
    payload: Payload,
    *,
    question: str,
    kind: str,
    api_key: Optional[str] = None,
    weather: Optional[dict] = None,
) -> ReportResult:
    """Run the daily analysis on ``payload`` and return the text plus sync flags.

    Raises ``AnalystError`` on Claude failure — the caller decides how to surface it.
    Dedup-cache behaviour is unchanged: this only assembles the ``run_analysis`` call
    that each channel already made.
    """
    text = await run_analysis(
        session, payload, user_id=user.id, question=question,
        kind=kind, api_key=api_key, weather=weather,
    )
    return ReportResult(text, payload.synced_today, payload.last_data_date)
