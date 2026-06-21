"""ORM models — history, cost tracking, and bot state.

Dates are stored as ISO strings (``YYYY-MM-DD``) to match the payload shape used
throughout the app. These are the persistence models only; the API/payload shape
lives in ``app.garmin.schemas`` and is mapped across in the repository.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DailyMetric(Base):
    """One row per day of recovery/sleep metrics. Past days are immutable, so
    this doubles as the day-level cache (serve from here instead of Garmin)."""

    __tablename__ = "daily_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, index=True)

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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    kind: Mapped[str] = mapped_column(String(16))          # report / deep / morning
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(String(512))


class BotState(Base):
    """Generic key/value bot state (e.g. last morning-report date). Replaces the
    old in-memory ``_sent_today`` + ``state.json``."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
