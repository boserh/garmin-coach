"""Add Garmin workout/schedule ids to planned_workouts

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-30 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('garmin_workout_id', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('garmin_schedule_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_column('garmin_schedule_id')
        batch_op.drop_column('garmin_workout_id')
