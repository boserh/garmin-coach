"""Add structured steps to planned_workouts

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('steps', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_column('steps')
