"""Dedup-cache keys and the shared context builders they hash.

The cache *itself* is the ``llm_cache`` DB table (``app.db.llm_cache``, PERF-02) so the
bot and the web process share hits; get/put happens in the async ``run_*`` wrappers,
which hold the DB session. This module holds the pure key functions (a sha256 of the
meaningful Claude inputs, with the volatile ``generated`` timestamp deliberately excluded)
plus the small context-shaping helpers (``_as_dict``, the fitness snapshot, the
cross-sport load budget) that both ``reports`` and ``plans`` feed to Claude — and that
therefore must be part of the key (the README pitfall: every piece of Claude context
must key the dedup cache).

Split out of the old flat ``analysis.service`` (CODE-01).
"""
import datetime as dt
import hashlib
import json
from typing import Optional, Union

from app.garmin.schemas import Payload

CACHE_TTL_S = 7 * 24 * 3600  # one week


def _as_dict(payload: Union[Payload, dict]) -> dict:
    d = payload.model_dump() if isinstance(payload, Payload) else payload
    # Strip per-point pace/HR series from activities — it's used only for single-activity
    # analysis (activity_payload/_segments), not for daily reports, and adds 5-6 KB per run.
    acts = d.get("recent_activities")
    if acts:
        d = {**d, "recent_activities": [
            {k: v for k, v in a.items() if k != "series"} for a in acts
        ]}
    return d


_FITNESS_KEYS = (
    "vo2max", "fitness_age",
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "endurance_score", "endurance_class",
    "acwr_pct", "acwr_feedback", "acute_load", "recovery_time_h",
    "readiness_score", "readiness_level",
    "hrv_baseline_low", "hrv_baseline_high",
    "resting_hr", "spo2_avg", "respiration_avg", "breathing_disruption_sev",
)


def _build_fitness_snapshot(ex: dict) -> Optional[dict]:
    """Filter a get_recent_extra coalesced dict down to the fitness keys used in analysis.
    Returns None when no relevant data is present (new user, no history)."""
    snap = {k: ex[k] for k in _FITNESS_KEYS if ex.get(k) is not None}
    return snap or None


MULTISPORT_WEEKS = 6   # how many ISO weeks of cross-sport load to feed as context (NF-05)


async def _build_multisport(session, user_id: int) -> Optional[dict]:
    """Cross-sport weekly training-load budget (NF-05) for the plan/adaptation/digest
    context: recent weekly load buckets (all sports) + a this-week-vs-last headline. Returns
    ``None`` when there's no non-run/other load to speak of. Pure math lives in
    ``app.multisport``; here we just fetch + shape."""
    from app import multisport
    from app.garmin import repository

    weekly = await repository.weekly_activity_load(session, user_id, weeks=MULTISPORT_WEEKS)
    if not weekly:
        return None
    today = dt.date.today()
    this_week = today.strftime("%G-W%V")
    prev_week = (today - dt.timedelta(days=7)).strftime("%G-W%V")
    return {
        "weeks": weekly,
        "this_week": multisport.budget_summary(weekly, this_week, prev_week),
    }


def _cache_key(data: dict, question: str, model: str, previous_report: Optional[dict] = None,
               weather: Optional[dict] = None,
               plan_today: Optional[list] = None,
               fitness: Optional[dict] = None,
               records: Optional[list] = None,
               norm: Optional[dict] = None) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "daily": data.get("daily"),
        "activities": data.get("recent_activities"),
        "planned": data.get("planned_runs"),
        "question": question,
        "model": model,
        "prev": previous_report,
        "weather": weather,
        "plan_today": plan_today,
        "fitness": fitness,
        "records": records,
        "norm": norm,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ask_cache_key(reports: list, question: str, model: str, recent_asks: list) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "reports": reports,
        "recent_asks": recent_asks,
        "question": question,
        "model": model,
        "ask": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _activity_cache_key(data: dict, model: str) -> str:
    blob = json.dumps({"activity": data, "model": model, "act": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _digest_cache_key(context: dict, model: str) -> str:
    """Key the weekly digest on the ISO week + the computed data slice (not ``today``),
    so a repeat within the same week/data is a cache hit — see the README pitfall: every
    piece of Claude context must be in the key."""
    material = {
        "iso_week": context.get("iso_week"),
        "week": context.get("week"),
        "weekly_volume": context.get("weekly_volume"),
        "compliance": context.get("compliance"),
        "recovery": context.get("recovery"),
        "fitness": context.get("fitness"),
        "multisport": context.get("multisport"),
        "goal": context.get("goal"),
        "records": context.get("records"),
        "model": model,
        "digest": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _compare_cache_key(context: dict, model: str) -> str:
    """Key the comparison on the two assembled windows + framing (not ``today`` alone), so a
    repeat within the same day/data is a cache hit — the README pitfall (all Claude context
    must key the dedup cache). The window date-ranges are inside current/past, so they key it."""
    material = {
        "weeks": context.get("weeks"),
        "years_back": context.get("years_back"),
        "current": context.get("current"),
        "past": context.get("past"),
        "model": model,
        "compare": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
