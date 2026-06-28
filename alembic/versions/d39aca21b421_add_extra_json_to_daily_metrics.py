"""Add extra json to daily_metrics

Revision ID: d39aca21b421
Revises: 225463f525fe
Create Date: 2026-06-28 08:41:40.494748
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd39aca21b421'
down_revision: Union[str, None] = '225463f525fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('daily_metrics', schema=None) as batch_op:
        batch_op.add_column(sa.Column('extra', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('daily_metrics', schema=None) as batch_op:
        batch_op.drop_column('extra')
