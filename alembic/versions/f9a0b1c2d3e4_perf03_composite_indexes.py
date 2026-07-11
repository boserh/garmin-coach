"""PERF-03 (index-audit slice): composite indexes for user-scoped reads

Revision ID: f9a0b1c2d3e4
Revises: e7f8a9b0c1d2
Create Date: 2026-07-11 13:40:00.000000

The frozen parts of PERF-03 (the Postgres switch) wait on opening ``/register``;
the cheap, deploy-independent index audit is done here. Hot reads all filter by a
scope column and order by a date/time — add the composite indexes they want.

``daily_metrics(user_id, date)`` is deliberately NOT added: the existing
``uq_daily_user_date`` UNIQUE constraint already provides that composite index.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_activities_user_date', 'activities', ['user_id', 'date'])
    op.create_index(
        'ix_report_logs_user_created', 'report_logs', ['user_id', 'created_at']
    )
    op.create_index(
        'ix_planned_workouts_plan_date', 'planned_workouts', ['plan_id', 'date']
    )


def downgrade() -> None:
    op.drop_index('ix_planned_workouts_plan_date', table_name='planned_workouts')
    op.drop_index('ix_report_logs_user_created', table_name='report_logs')
    op.drop_index('ix_activities_user_date', table_name='activities')
