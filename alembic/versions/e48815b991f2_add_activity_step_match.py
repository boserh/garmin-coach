"""Add step_match to activities (NF-14 step-level plan-vs-actual)

Revision ID: e48815b991f2
Revises: f19ea8917ca6
Create Date: 2026-07-21 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e48815b991f2'
down_revision: Union[str, None] = 'f19ea8917ca6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('step_match', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('step_match')
