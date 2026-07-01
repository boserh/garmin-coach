"""Add garmin_template_id to planned_workouts (strength templates)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-02 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('garmin_template_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_column('garmin_template_id')
