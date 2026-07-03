"""add_plan_actual_matching

Revision ID: 1efa5d6615f3
Revises: d0e1f2a3b4c5
Create Date: 2026-07-03 17:42:50.108522
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1efa5d6615f3'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('completed_activity_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('match_info', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_column('match_info')
        batch_op.drop_column('completed_activity_id')
