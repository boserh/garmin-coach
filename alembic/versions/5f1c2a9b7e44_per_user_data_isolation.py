"""Per-user data isolation: add user_id to data tables; composite uniques.

Existing rows are backfilled to the first user (lowest users.id). user_id stays
nullable on the metric/activity/report tables (legacy rows tolerate NULL); bot_state
gets a composite (user_id, key) primary key and is rebuilt.

Revision ID: 5f1c2a9b7e44
Revises: 23a723972a13
Create Date: 2026-06-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5f1c2a9b7e44"
down_revision: Union[str, None] = "23a723972a13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- add nullable user_id columns ---
    with op.batch_alter_table("daily_metrics") as b:
        b.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("activities") as b:
        b.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("report_logs") as b:
        b.add_column(sa.Column("user_id", sa.Integer(), nullable=True))

    # --- backfill existing data to the first user, if one exists ---
    for tbl in ("daily_metrics", "activities", "report_logs"):
        op.execute(
            f"UPDATE {tbl} SET user_id = (SELECT min(id) FROM users) "
            f"WHERE user_id IS NULL"
        )

    # --- swap single-column uniques for per-user composite uniques ---
    with op.batch_alter_table("daily_metrics") as b:
        b.drop_index("ix_daily_metrics_date")
        b.create_index("ix_daily_metrics_date", ["date"], unique=False)
        b.create_index("ix_daily_metrics_user_id", ["user_id"], unique=False)
        b.create_unique_constraint("uq_daily_user_date", ["user_id", "date"])
    with op.batch_alter_table("activities") as b:
        b.drop_index("ix_activities_activity_id")
        b.create_index("ix_activities_activity_id", ["activity_id"], unique=False)
        b.create_index("ix_activities_user_id", ["user_id"], unique=False)
        b.create_unique_constraint("uq_activity_user_aid", ["user_id", "activity_id"])
    with op.batch_alter_table("report_logs") as b:
        b.create_index("ix_report_logs_user_id", ["user_id"], unique=False)

    # --- bot_state: rebuild with a composite (user_id, key) primary key ---
    op.rename_table("bot_state", "bot_state_old")
    op.create_table(
        "bot_state",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "key"),
    )
    op.execute(
        "INSERT INTO bot_state (user_id, key, value, updated_at) "
        "SELECT COALESCE((SELECT min(id) FROM users), 1), key, value, updated_at "
        "FROM bot_state_old"
    )
    op.drop_table("bot_state_old")


def downgrade() -> None:
    op.rename_table("bot_state", "bot_state_new")
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.execute(
        "INSERT OR REPLACE INTO bot_state (key, value, updated_at) "
        "SELECT key, value, updated_at FROM bot_state_new"
    )
    op.drop_table("bot_state_new")

    with op.batch_alter_table("report_logs") as b:
        b.drop_index("ix_report_logs_user_id")
        b.drop_column("user_id")
    with op.batch_alter_table("activities") as b:
        b.drop_constraint("uq_activity_user_aid", type_="unique")
        b.drop_index("ix_activities_user_id")
        b.drop_index("ix_activities_activity_id")
        b.create_index("ix_activities_activity_id", ["activity_id"], unique=True)
        b.drop_column("user_id")
    with op.batch_alter_table("daily_metrics") as b:
        b.drop_constraint("uq_daily_user_date", type_="unique")
        b.drop_index("ix_daily_metrics_user_id")
        b.drop_index("ix_daily_metrics_date")
        b.create_index("ix_daily_metrics_date", ["date"], unique=True)
        b.drop_column("user_id")
