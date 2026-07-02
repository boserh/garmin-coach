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
    __table_args__ = (UniqueConstraint("user_id", "activity_id", name="uq_activity_user_aid"),)

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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReportLog(Base):
    """One row per Claude analysis call — for cost tracking and metrics."""

    __tablename__ = "report_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    kind: Mapped[str] = mapped_column(String(16))          # report / deep / morning / ask
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)  # served from dedup cache
    error: Mapped[Optional[str]] = mapped_column(String(512))
    question: Mapped[Optional[str]] = mapped_column(Text)      # the asked question / prompt
    report_text: Mapped[Optional[str]] = mapped_column(Text)  # the delivered report


class BotState(Base):
    """Generic key/value bot state (e.g. last morning-report date). Replaces the
    old in-memory ``_sent_today`` + ``state.json``."""

    __tablename__ = "bot_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


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


class PlannedWorkout(Base):
    """One dated session of a TrainingPlan. ``description`` carries the free-text
    prescription (what to do + target pace/effort); ``status`` tracks progress."""

    __tablename__ = "planned_workouts"

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
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned/done/skipped
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
