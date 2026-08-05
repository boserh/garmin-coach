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
               norm: Optional[dict] = None,
               subjective: Optional[dict] = None,
               health_alerts: Optional[dict] = None,
               fueling: Optional[dict] = None,
               today: Optional[str] = None,
               intensity: Optional[dict] = None,
               athlete_profile: Optional[dict] = None) -> str:
    # ``today`` is the user's own date (their timezone, ST-14) when the caller knows it —
    # it is part of the prompt (and of every relative-day label built from it), so it must
    # be part of the key. Falls back to the process date for callers without a user.
    material = {
        "today": today or dt.date.today().isoformat(),
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
        "subjective": subjective,
        "health_alerts": health_alerts,
        "fueling": fueling,
        # NF-24: the intensity block is prompt context, so it must be part of the key —
        # otherwise a week that just drifted into the grey zone would keep returning
        # yesterday's report, which is exactly the trap the backlog warns about.
        "intensity": intensity,
        # EP-18: without the profile in the key, editing a fact would change the prompt but
        # not the hash — the coach would "learn" something and keep serving the old report.
        "athlete_profile": athlete_profile,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ask_cache_key(reports: list, question: str, model: str, recent_asks: list,
                   last_data_date: Optional[str] = None,
                   athlete_profile: Optional[dict] = None) -> str:
    # EP-09: keyed on a coarse daily-data slice (last_data_date — the most recent stored
    # daily_metrics date, a pure-DB proxy for "has anything changed") rather than the
    # calendar date alone, so a repeat question before today's data has synced is still a
    # cache hit instead of paying for an identical tool-use run. Falls back to today's date
    # for a brand-new user with no stored days yet.
    material = {
        "last_data_date": last_data_date or dt.date.today().isoformat(),
        "reports": reports,
        "recent_asks": recent_asks,
        "question": question,
        "model": model,
        "ask": True,
        # EP-18: the profile is part of what /ask reads, so it keys the cache too.
        "athlete_profile": athlete_profile,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _activity_cache_key(data: dict, model: str) -> str:
    blob = json.dumps({"activity": data, "model": model, "act": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _checkup_cache_key(data: dict, model: str) -> str:
    """Keys on the checkup's own payload (incl. its ``history`` slice, since that's part
    of what the model reads — the README pitfall) + model. No ``today``/date-only key is
    needed: a checkup's own data never changes on its own, only when edited."""
    blob = json.dumps({"checkup": data, "model": model, "chk": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _supplement_cache_key(data: dict, model: str) -> str:
    """Keys on the active-supplement list + recent checkup categories (both part of the
    prompt — the README pitfall) + model. Changing/adding/stopping a supplement changes
    the payload and so naturally busts the cache; no date component needed."""
    blob = json.dumps({"supplements": data, "model": model, "supp": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _context_cache_key(kind: str, context: dict, model: str, fields: tuple) -> str:
    """Generic dedup-cache key for a context-driven narration (A2): pick ``fields`` from
    ``context``, add the model and a ``{kind: True}`` marker, sha256 the JSON. Replaces the
    five near-identical ``_digest/_insights/_wrapped/_race/_compare`` key builders that all
    had this exact shape.

    The README pitfall lives here: **every piece of Claude context must be in the key**, so
    each caller's ``fields`` must list every context field the model actually reads. The
    volatile ``generated``/``today`` values are deliberately excluded so a same-day/same-week
    repeat over identical data is a cache hit rather than a paid re-run. ``sort_keys`` makes
    the field order irrelevant, so this yields the same hash the hand-written builders did.
    """
    material = {f: context.get(f) for f in fields}
    material["model"] = model
    material[kind] = True
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Per-narration field lists — the exact context each model reads (see _context_cache_key).
_DIGEST_KEY_FIELDS = (
    "iso_week", "week", "weekly_volume", "compliance", "recovery",
    "fitness", "multisport", "goal", "goal_projection", "efficiency", "records",
    "sleep_regularity",
    "intensity",   # NF-24 — prompt context, therefore key material
)
# NF-28's lifestyle findings are a separate context key, so they must be listed here too —
# without it a newly-logged tag would change the prompt but not the hash, and /insights
# would keep serving the pre-lifestyle text (the backlog's cross-cutting trap).
_INSIGHTS_KEY_FIELDS = ("window_days", "findings", "lifestyle_findings")
_WRAPPED_KEY_FIELDS = ("period", "start", "end", "stats", "records")
_RACE_KEY_FIELDS = (
    "goal", "target_date", "target_dist_km", "fitness", "recent_sessions", "weather",
)
_COMPARE_KEY_FIELDS = ("weeks", "years_back", "current", "past")


def _digest_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("digest", context, model, _DIGEST_KEY_FIELDS)


def _insights_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("insights", context, model, _INSIGHTS_KEY_FIELDS)


def _wrapped_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("wrapped", context, model, _WRAPPED_KEY_FIELDS)


def _race_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("race", context, model, _RACE_KEY_FIELDS)


def _compare_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("compare", context, model, _COMPARE_KEY_FIELDS)
