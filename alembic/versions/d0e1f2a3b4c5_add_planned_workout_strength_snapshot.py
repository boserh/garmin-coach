"""add planned_workout.strength_snapshot

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-02 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts') as batch_op:
        batch_op.add_column(sa.Column('strength_snapshot', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts') as batch_op:
        batch_op.drop_column('strength_snapshot')
