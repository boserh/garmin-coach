"""add planned_workout.strength_plan

Revision ID: c9d0e1f2a3b4
Revises: f6a7b8c9d0e1
Create Date: 2026-07-02 15:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts') as batch_op:
        batch_op.add_column(sa.Column('strength_plan', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts') as batch_op:
        batch_op.drop_column('strength_plan')
