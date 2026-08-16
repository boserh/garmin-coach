"""Fix recovery_time_h: stored history is in MINUTES, not hours

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-16 12:00:00.000000

Garmin's training-readiness ``recoveryTime`` is minutes; it went into
``daily_metrics.extra["recovery_time_h"]`` unconverted, so a normal 19-hour recovery reached
every prompt as "1164 годин" (48 days) and the plan-adaptation job rebuilt the week around
it. The write path is fixed in ``app.garmin.service.recovery_hours``; every row already
stored came from that one buggy path, so the conversion here is unconditional — no
heuristic, no ambiguity about which unit a given row is in.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches app.garmin.service.RECOVERY_MAX_H — a converted value beyond it was never a
# recovery time, so it is dropped rather than carried forward as a fact.
RECOVERY_MAX_H = 168.0

_daily = sa.table(
    "daily_metrics",
    sa.column("id", sa.Integer),
    sa.column("extra", sa.JSON),
)


def _rescale(factor: float, cap: bool) -> None:
    """Rewrite every stored ``recovery_time_h`` by ``factor``. Goes through SQLAlchemy's
    JSON type rather than raw SQL so it works on both SQLite (TEXT) and Postgres (JSON)."""
    conn = op.get_bind()
    rows = conn.execute(sa.select(_daily.c.id, _daily.c.extra)).fetchall()
    for row_id, extra in rows:
        if not isinstance(extra, dict):
            continue
        val = extra.get("recovery_time_h")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        scaled = round(val * factor, 1)
        updated = dict(extra)
        if cap and scaled > RECOVERY_MAX_H:
            updated.pop("recovery_time_h")
        else:
            updated["recovery_time_h"] = scaled
        conn.execute(
            sa.update(_daily).where(_daily.c.id == row_id).values(extra=updated)
        )


def upgrade() -> None:
    _rescale(1 / 60, cap=True)


def downgrade() -> None:
    # Rows dropped as out-of-range on the way up cannot be restored — they were never a
    # usable value in either unit.
    _rescale(60, cap=False)
