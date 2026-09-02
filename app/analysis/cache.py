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


# Fields we fetch and store but never put in front of the analyst. Three reasons, and
# every entry is one of them: (a) the system prompt documents no meaning for it, so the
# model is left to invent one; (b) it duplicates a value that reaches the analyst through
# `fitness`, which is the field the prompt actually describes; (c) it duplicates a modelled
# column of the same day (overnight_hrv == hrv_avg, bb_change == bb_charged).
#
# This is about ATTENTION, not tokens. A morning report is a needle-in-haystack task — one
# day's session among twenty activities — and every undocumented number is another straw.
# Nothing here is lost: the pure modules (baselines, intensity, records, health) read these
# straight from the DB, never from the prompt, so trimming the payload changes what the
# model reads and nothing else.
_DROP_DAILY_EXTRA = frozenset({
    # (c) duplicates of a modelled column on the same row
    "overnight_hrv", "bb_change", "awake_count",
    # (a) undocumented, and not recovery signal a daily report acts on
    "avg_hr_sleep", "avg_sleep_stress", "restless_moments", "hrv_5min_high",
    "hrv_weekly_avg", "spo2_low", "min_hr", "bb_high", "bb_low",
    "floors_up", "distance_m", "active_kcal", "moderate_min", "vigorous_min",
    # (b) near-constant day to day, and carried properly by `fitness`
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "endurance_score", "endurance_class", "vo2max",
})

# Same rule for an activity row. `series` is the big one (5-6 KB per run) and was always
# stripped here; `zones` is the surprising one — NF-24's time-in-zone is real data, but the
# analyst is told nothing about it (the intensity findings reach it as `intensity`, computed
# in Python), so it arrives as five unexplained integers per activity. Coordinates and gear
# are read from the DB by the weather and gear code, never from the prompt.
_DROP_ACTIVITY = frozenset({"series", "zones", "start_lat", "start_lon", "gear_id"})

# How far back the analyst sees activities. `activity_limit` is a COUNT, so a 3-day morning
# report was being handed 20 activities spanning 24 days — a fortnight of holiday hiking
# around the one session that mattered. The window follows the payload's own `window_days`
# (morning 3, /report 7, /deep 14) with a floor, so a deep dive still gets its history.
ACTIVITY_CONTEXT_MIN_DAYS = 10
# ...but never leave the analyst blind: an athlete who hasn't trained in a fortnight still
# needs "the last thing you did was X", so keep this many newest rows whatever their date.
ACTIVITY_CONTEXT_MIN_KEEP = 3


def _trim_activity(a: dict) -> dict:
    return {k: v for k, v in a.items() if k not in _DROP_ACTIVITY}


def _trim_day(d: dict) -> dict:
    extra = d.get("extra")
    if not isinstance(extra, dict):
        return d
    return {**d, "extra": {k: v for k, v in extra.items() if k not in _DROP_DAILY_EXTRA}}


def _recent_enough(acts: list, today: Optional[str], window_days) -> list:
    """Activities within the context window, newest first, never fewer than
    ``ACTIVITY_CONTEXT_MIN_KEEP``. ``today`` unset → no date trim (the caller has no clock)."""
    day = dt.date.fromisoformat(today[:10]) if today else None
    if day is None:
        return acts
    try:
        window = max(int(window_days or 0), ACTIVITY_CONTEXT_MIN_DAYS)
    except (TypeError, ValueError):
        window = ACTIVITY_CONTEXT_MIN_DAYS
    cutoff = (day - dt.timedelta(days=window)).isoformat()
    kept = [a for a in acts if (a.get("date") or "") >= cutoff]
    return kept if len(kept) >= ACTIVITY_CONTEXT_MIN_KEEP else acts[:ACTIVITY_CONTEXT_MIN_KEEP]


def _as_dict(payload: Union[Payload, dict], *, today: Optional[str] = None) -> dict:
    """The payload as the analyst sees it — and, identically, as the dedup cache keys it.

    Both callers in ``reports`` go through here for exactly that reason: the README pitfall
    is that every piece of Claude context must be part of the key, and the mirror of it is
    that anything trimmed out of the prompt must be trimmed out of the key too, or an
    invisible field starts busting the cache.
    """
    d = payload.model_dump() if isinstance(payload, Payload) else payload
    daily = d.get("daily")
    if daily:
        d = {**d, "daily": [_trim_day(x) if isinstance(x, dict) else x for x in daily]}
    acts = d.get("recent_activities")
    if acts:
        kept = _recent_enough(acts, today, d.get("window_days"))
        d = {**d, "recent_activities": [_trim_activity(a) for a in kept]}
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

# The half of _FITNESS_KEYS Garmin recomputes EVERY day — its Training Readiness DTO
# (score/level plus the load block). The rest of the snapshot is fine to coalesce over
# three weeks (VO2max and race predictions only move after a qualifying activity, the
# HRV baseline corridor drifts over weeks), but these describe "how ready am I right
# now", and SYSTEM_PLAN_ADAPT turns a low readiness_score straight into a deload.
# Coalesced without a bound, a single bad day could still be presented as today's state
# three weeks later — so they carry a max age of their own.
_FITNESS_VOLATILE_KEYS = frozenset({
    "readiness_score", "readiness_level",
    "recovery_time_h", "acute_load", "acwr_pct", "acwr_feedback",
})

# How old a volatile value may be before it is dropped from the snapshot instead of
# being passed off as current. Three days keeps a normal gap (a night without the watch)
# usable while making "readiness from a fortnight ago" impossible.
FITNESS_VOLATILE_MAX_AGE_DAYS = 3


def _age_days(date_s: Optional[str], today: dt.date) -> Optional[int]:
    try:
        return (today - dt.date.fromisoformat(date_s)).days
    except (TypeError, ValueError):
        return None


def _build_fitness_snapshot(ex: dict, dates: Optional[dict] = None, *,
                            today: Optional[dt.date] = None) -> Optional[dict]:
    """Filter a get_recent_extra coalesced dict down to the fitness keys used in analysis.
    Returns None when no relevant data is present (new user, no history).

    ``dates`` — ``{key: "YYYY-MM-DD"}``, the day each coalesced value came from (from
    ``repository.get_recent_extra_dated``). With it, the volatile keys above are dropped
    once they are older than ``FITNESS_VOLATILE_MAX_AGE_DAYS``, and a snapshot whose
    volatile block is not from today gains an ``asof`` date so the prompt can say how
    fresh "readiness" actually is. Without it the snapshot is built exactly as before —
    the callers that have no session to fetch dates with keep working unchanged.
    """
    snap = {k: ex[k] for k in _FITNESS_KEYS if ex.get(k) is not None}
    if not dates:
        return snap or None

    today = today or dt.date.today()
    ages = {}
    for key in list(snap):
        if key not in _FITNESS_VOLATILE_KEYS:
            continue
        age = _age_days(dates.get(key), today)
        if age is None or age > FITNESS_VOLATILE_MAX_AGE_DAYS:
            del snap[key]          # stale (or undatable) — absent beats wrong
        else:
            # Daily rows are keyed by the PROCESS date while ``today`` may be the
            # athlete's own (ST-14), so a value can look like it is from tomorrow.
            # Clamp rather than stamp a snapshot as older-than-fresh in reverse.
            ages[key] = max(age, 0)
    if not snap:
        return None
    if ages and max(ages.values()) > 0:
        # Stamp the OLDEST surviving volatile value: the snapshot is only as current as
        # its weakest part, and no stamp at all must keep meaning "this is today".
        oldest = max(ages, key=lambda k: ages[k])
        snap["asof"] = dates[oldest]
    return snap


async def build_fitness_context(
    session, user_id: int, *, days: int = 21, today: Optional[dt.date] = None
) -> Optional[dict]:
    """The Garmin fitness snapshot every LLM surface feeds Claude: coalesce the last
    ``days`` of ``DailyMetric.extra``, then hand it to :func:`_build_fitness_snapshot`
    WITH the per-value dates, so a stale readiness can't pose as today's.

    One helper rather than a fetch+build pair at each call site — the same reason
    ``profile_db.build_context`` exists: "what the coach knows" must not drift between
    the morning report, the digest and the adaptation.
    """
    from app.garmin import repository

    dated = await repository.get_recent_extra_dated(session, user_id, days, today)
    return _build_fitness_snapshot(
        {k: v for k, (v, _d) in dated.items()},
        {k: d for k, (_v, d) in dated.items()},
        today=today,
    )


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
               athlete_profile: Optional[dict] = None,
               away: Optional[dict] = None) -> str:
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
        # NF-34: same rule — declaring a vacation must produce a NEW report today, not a
        # cache hit on the one written before the coach knew.
        "away": away,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ask_cache_key(reports: list, question: str, model: str, recent_asks: list,
                   last_data_date: Optional[str] = None,
                   athlete_profile: Optional[dict] = None,
                   away: Optional[dict] = None) -> str:
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
        "away": away,          # NF-34 — same reason
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
    "strength",    # NF-27 — same
    "away",        # NF-34 — declaring a vacation must not return the pre-vacation digest
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
# NF-23: ``activity_id`` is in the key on purpose — it makes a repeat ``/race done <id>`` a
# cache HIT instead of a second paid Opus/Sonnet call for a race whose numbers cannot change
# (the ticket calls this out as the cross-cutting trap).
_RACE_DEBRIEF_KEY_FIELDS = (
    "activity_id", "race", "debrief", "buildup", "weather", "subjective",
)


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


def _race_debrief_cache_key(context: dict, model: str) -> str:
    return _context_cache_key("race_debrief", context, model, _RACE_DEBRIEF_KEY_FIELDS)
