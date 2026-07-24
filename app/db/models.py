"""ORM models — history, cost tracking, and bot state.

Dates are stored as ISO strings (``YYYY-MM-DD``) to match the payload shape used
throughout the app. These are the persistence models only; the API/payload shape
lives in ``app.garmin.schemas`` and is mapped across in the repository.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A web-login account that owns its own Garmin/Claude credentials and data.

    Login is via ``email`` + bcrypt ``password_hash``. Upstream credentials are
    stored encrypted (Fernet tokens, see ``app.core.crypto``); ``telegram_chat_id``
    is a routing identifier (kept plaintext + indexed so the bot can look a user up
    by an incoming chat id), not a secret."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Self-registered accounts start unapproved and cannot log in until an admin
    # approves them. Admin/CLI-created accounts are approved on creation.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    # An admin can deactivate an account (block login + bot, keep its data) and
    # reactivate it later. Distinct from approval: active=False is a deliberate off.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Encrypted upstream credentials (Fernet tokens); null until the user fills them in.
    garmin_email_enc: Mapped[Optional[str]] = mapped_column(Text)
    garmin_password_enc: Mapped[Optional[str]] = mapped_column(Text)
    anthropic_key_enc: Mapped[Optional[str]] = mapped_column(Text)
    garth_token_enc: Mapped[Optional[str]] = mapped_column(Text)  # dumped garth session

    telegram_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, index=True)

    # Location for the morning report's weather lookup. ``weather_location`` is the
    # geocoded display name ("City, Country"); lat/lon are resolved once on save (see
    # app.weather.geocode) so the morning job needs no extra geocoding. Null → no weather.
    weather_location: Mapped[Optional[str]] = mapped_column(String(128))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)

    # Master switch for pushing plan workouts to the Garmin-Connect calendar. Off → all
    # automatic sync hooks skip this user (the manual push-plan CLI still works). Lets a
    # user generate + validate a plan first, then enable pushing.
    garmin_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Master switch for the adaptive-plan hooks (weekly review + morning nudge, EP-02).
    # Off → plan_adapt_job and the morning readiness check skip this user entirely.
    plan_adapt_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Master switch for proactive health alerts (EP-08 — sustained recovery anomalies vs
    # the user's personal baseline). Off → the morning tick's health check skips this user.
    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # ST-14: this user's own IANA timezone (validated via zoneinfo.ZoneInfo on save).
    # Per-user checks in bot/jobs.py (the morning window, once-a-day/week/month bot_state
    # guard dates) read this instead of the hardcoded Europe/Warsaw — a traveling user or a
    # second user outside CET gets their morning report in THEIR morning, not the process's.
    # The run_daily-scheduled jobs themselves (weekly digest hour, plan-sync hour, ...) stay
    # on the process timezone in v1 — see CLAUDE.md ST-14 notes.
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Warsaw")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DailyMetric(Base):
    """One row per day of recovery/sleep metrics. Past days are immutable, so
    this doubles as the day-level cache (serve from here instead of Garmin)."""

    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_daily_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)

    sleep_score: Mapped[Optional[int]] = mapped_column(Integer)
    sleep_h: Mapped[Optional[float]] = mapped_column(Float)
    deep_h: Mapped[Optional[float]] = mapped_column(Float)
    rem_h: Mapped[Optional[float]] = mapped_column(Float)
    light_h: Mapped[Optional[float]] = mapped_column(Float)
    awake_h: Mapped[Optional[float]] = mapped_column(Float)
    hrv_avg: Mapped[Optional[int]] = mapped_column(Integer)
    hrv_status: Mapped[Optional[str]] = mapped_column(String(32))
    stress_avg: Mapped[Optional[int]] = mapped_column(Integer)
    stress_max: Mapped[Optional[int]] = mapped_column(Integer)
    bb_charged: Mapped[Optional[int]] = mapped_column(Integer)
    bb_drained: Mapped[Optional[int]] = mapped_column(Integer)
    # Extra scalar metrics we fetch but don't model as columns (RHR, SpO2, respiration,
    # skin-temp deviation, training readiness, ACWR, HRV detail …). Compact dict, no arrays.
    extra: Mapped[Optional[dict]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ActivityRecord(Base):
    """One row per Garmin activity. ``exercises`` holds the strength-set
    breakdown (muscle groups / per-exercise counts) as JSON."""

    __tablename__ = "activities"
    __table_args__ = (
        UniqueConstraint("user_id", "activity_id", name="uq_activity_user_aid"),
        # PERF-03 index audit: reads filter user_id and order by date (see repository).
        Index("ix_activities_user_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    activity_id: Mapped[int] = mapped_column(BigInteger, index=True)
    date: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    type: Mapped[Optional[str]] = mapped_column(String(64))
    dur_min: Mapped[Optional[float]] = mapped_column(Float)
    dist_km: Mapped[Optional[float]] = mapped_column(Float)
    avg_hr: Mapped[Optional[int]] = mapped_column(Integer)
    max_hr: Mapped[Optional[int]] = mapped_column(Integer)
    load: Mapped[Optional[float]] = mapped_column(Float)
    exercises: Mapped[Optional[dict]] = mapped_column(JSON)
    # Per-point pace/HR series for runs ([{d, p, hr}, ...]); null for other types.
    series: Mapped[Optional[list]] = mapped_column(JSON)
    # Claude's on-demand analysis of this activity (/activity); null until requested.
    analysis: Mapped[Optional[str]] = mapped_column(Text)
    # Post-run subjective check-in (EP-12): {rpe: 1-10, pain?: bool, note?: str} — the
    # runner's felt effort + any niggle, captured via inline buttons after auto-analysis
    # (re-tap overwrites). Null until answered; silence is a valid non-answer.
    subjective: Mapped[Optional[dict]] = mapped_column(JSON)
    # NF-14: step-level plan-vs-actual — {steps_hit, steps_total, misses:[{step,planned,
    # actual}]} from app.stepmatch, computed once the run is matched to a session we
    # pushed with structured steps. Null when there's nothing structured to compare
    # (free run, no match, or the session predates the feature).
    step_match: Mapped[Optional[dict]] = mapped_column(JSON)
    # ST-17: user-hidden (a duplicate watch+phone record, a broken-GPS track, a stray
    # activity synced from someone else's device). A hidden row is excluded from every
    # list / aggregate / record / plan-match and stays hidden after the next Garmin sync
    # (upsert_activity never resets it). Kept, not deleted, so a resync can't resurrect it.
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReportLog(Base):
    """One row per Claude analysis call — for cost tracking and metrics."""

    __tablename__ = "report_logs"
    # PERF-03 index audit: cost totals + /me history filter user_id, order by created_at.
    __table_args__ = (Index("ix_report_logs_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # report/deep/morning/ask/activity/plan*/adapt/digest/strength
    kind: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)  # served from dedup cache
    error: Mapped[Optional[str]] = mapped_column(String(512))
    question: Mapped[Optional[str]] = mapped_column(Text)      # the asked question / prompt
    report_text: Mapped[Optional[str]] = mapped_column(Text)  # the delivered report
    # EP-09: how many tool-use round trips /ask's agent loop took (null for every other
    # kind, and for an /ask served from the dedup cache — no fresh round happened).
    tool_rounds: Mapped[Optional[int]] = mapped_column(Integer)


class PersonalRecord(Base):
    """One personal-best milestone (EP-14). We keep the *history* of records, not just
    the current best — each time a category is beaten a new row is inserted, carrying the
    ``previous_value`` it dethroned. ``value`` semantics depend on ``kind``: pace records
    (``fastest_5k`` …) and race predictions (``race_5k`` …) are *lower is better*, the rest
    higher. ``date`` is when the record was achieved (an activity date, the daily-fetch date
    for VO2max/race predictions, or the last-run date of a record week) — the announce gate
    keys off it, so a backfill of old bests dates them in the past and stays silent."""

    __tablename__ = "personal_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float] = mapped_column(Float)
    previous_value: Mapped[Optional[float]] = mapped_column(Float)
    # The activity that set the record (DB id), when the record derives from one. Null for
    # week/VO2max/race-prediction records that aren't tied to a single activity.
    activity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activities.id"), nullable=True
    )
    date: Mapped[str] = mapped_column(String(10), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LlmCache(Base):
    """Cross-process Claude dedup cache (PERF-02). Replaces ``claude_cache.json``:
    a module-level dict per process meant the bot and the web app each paid for the
    same Claude call, and whole-file rewrites silently dropped the other process's
    entries. ``key`` is the sha256 hex from ``analysis.service._cache_key`` (and
    siblings) — key semantics unchanged; ``expires_at`` is epoch seconds, purged
    lazily on write (see ``app.db.llm_cache``)."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BotState(Base):
    """Generic key/value bot state (e.g. last morning-report date). Replaces the
    old in-memory ``_sent_today`` + ``state.json``.

    ``value`` is ``Text`` (not a short varchar) because EP-02 also stores a pending
    plan-adaptation proposal here (serialized ``PlanOp`` JSON — steps and all)."""

    __tablename__ = "bot_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class JobRun(Base):
    """OPS-04: one row per run of a per-user background-job branch (morning tick + the five
    run_daily jobs), so "did plan_sync run yesterday and how did it end?" is a web read, not
    an ssh+grep. ``status`` is ok/skip/error; ``detail`` is a short reason ("sent" / "outside
    window" / "MFARequired" / a traceback tail ≤512). The 20-min morning tick's routine
    ok/skip results are AGGREGATED into one row per user/day (``count`` = number of ticks,
    ``run_date`` = that local date) so the log isn't flooded — notable outcomes (a report
    actually sent, an MFA gate) and errors get their own rows. Rows older than 30 days are
    purged lazily on write (like llm_cache)."""

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_user_started", "user_id", "started_at"),
        Index("ix_job_runs_job_started", "job", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(8))          # ok / skip / error
    detail: Mapped[Optional[str]] = mapped_column(String(512))
    count: Mapped[int] = mapped_column(Integer, default=1)  # aggregated tick count
    run_date: Mapped[Optional[str]] = mapped_column(String(10))  # local date of the run
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class TrainingPlan(Base):
    """A generated running training program for a goal. One active plan per user
    (creating a new one archives the previous). Holds the goal/params, the intake
    answers, and Claude's approach summary; the dated sessions live in PlannedWorkout."""

    __tablename__ = "training_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(String(32))          # first_5k / faster_5k / ...
    goal_label: Mapped[Optional[str]] = mapped_column(String(128))
    target_date: Mapped[Optional[str]] = mapped_column(String(10))
    start_date: Mapped[Optional[str]] = mapped_column(String(10))
    days_per_week: Mapped[Optional[int]] = mapped_column(Integer)
    intensity: Mapped[Optional[str]] = mapped_column(String(16))   # easy / moderate / hard
    intake: Mapped[Optional[dict]] = mapped_column(JSON)           # the answers given
    summary: Mapped[Optional[str]] = mapped_column(Text)           # Claude's approach overview
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkoutStatus:
    """Canonical status values for PlannedWorkout.status — single source of truth used by
    matching.py, plan_sync.py, and the plan templates."""
    PLANNED = "planned"
    DONE = "done"
    PARTIAL = "partial"   # completed but distance off by > DIST_PARTIAL_THRESH
    MISSED = "missed"     # date passed with no matching activity
    SKIPPED = "skipped"   # explicitly skipped by the user (chat op or bot button)


class PlannedWorkout(Base):
    """One dated session of a TrainingPlan. ``description`` carries the free-text
    prescription (what to do + target pace/effort); ``status`` tracks progress."""

    __tablename__ = "planned_workouts"
    # PERF-03 index audit: sessions are read by plan_id ordered/filtered by date.
    __table_args__ = (Index("ix_planned_workouts_plan_date", "plan_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("training_plans.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    week: Mapped[Optional[int]] = mapped_column(Integer)
    type: Mapped[Optional[str]] = mapped_column(String(16))  # easy/long/tempo/intervals/rest/cross
    dist_km: Mapped[Optional[float]] = mapped_column(Float)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Structured step breakdown (warmup/run/recovery/cooldown/repeat, pace ranges) — both
    # richer detail and a future Garmin-Connect workout export. See schemas.PlanStep.
    steps: Mapped[Optional[list]] = mapped_column(JSON)
    # Garmin-Connect calendar export: the created workout + schedule ids once this
    # session has been pushed to the watch (null = not pushed). Makes push idempotent
    # and lets edits/archival unschedule what we put there.
    garmin_workout_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    garmin_schedule_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # A saved Garmin workout to CLONE from (e.g. a strength Day 1/Day 2 template). When
    # set, push creates OUR OWN copy of it and schedules that (like a run) — the user's
    # template is never scheduled or deleted; cleanup removes only our copy.
    garmin_template_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Strength exercise swaps to apply when cloning the template on push, e.g.
    # [{"from": "HYPEREXTENSION", "to": "DEADLIFT", "exercise": null, "reps": null,
    # "weight_kg": null}] — set by a chat edit ("заміни гіперекстензію на станову"),
    # validated against app.garmin.exercises. See workout_export.apply_exercise_edits.
    exercise_edits: Mapped[Optional[list]] = mapped_column(JSON)
    # A from-scratch generated strength session (no template to clone): a compact dict
    # {name?, warmup_s?, blocks:[{reps(sets), rest_s?, exercises:[{category, exercise?,
    # reps?, weight_kg?}]}]} produced by the LLM (categories validated against the Garmin
    # catalog). On push, workout_export.build_strength_workout turns it into a native
    # strength workout DTO — no Day 1/2 clone needed. Takes precedence over
    # garmin_template_id when both are set.
    strength_plan: Mapped[Optional[dict]] = mapped_column(JSON)
    # Display-only cache of a CLONED template's contents, snapshotted at plan-build time
    # ({name?, exercises:[{category, exercise?, reps?}]}) so /plan renders the exercise
    # accordion from the DB instead of re-fetching the Garmin template on every load.
    # Not used on push (the real template is cloned live). Null for run/from-scratch days.
    strength_snapshot: Mapped[Optional[dict]] = mapped_column(JSON)
    # Plan/actual matching: the ActivityRecord (by DB id) that satisfied this session, set
    # by matching.match_activities. Null until matched. See WorkoutStatus for status values.
    completed_activity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activities.id"), nullable=True
    )
    # Snapshot of the match at the time it was made: distances, pace comparison.
    # {dist_delta_km, actual_dist_km, activity_date, actual_pace_minkm?, plan_pace_minkm?}
    match_info: Mapped[Optional[dict]] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), default="planned")  # see WorkoutStatus
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
