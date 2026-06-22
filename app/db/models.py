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

    # Encrypted upstream credentials (Fernet tokens); null until the user fills them in.
    garmin_email_enc: Mapped[Optional[str]] = mapped_column(Text)
    garmin_password_enc: Mapped[Optional[str]] = mapped_column(Text)
    anthropic_key_enc: Mapped[Optional[str]] = mapped_column(Text)
    garth_token_enc: Mapped[Optional[str]] = mapped_column(Text)  # dumped garth session

    telegram_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, index=True)

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
