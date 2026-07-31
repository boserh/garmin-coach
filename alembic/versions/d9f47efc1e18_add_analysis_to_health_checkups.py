"""Add analysis to health_checkups

Revision ID: d9f47efc1e18
Revises: 7694e3a5a6aa
Create Date: 2026-07-31 05:51:19.646270
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd9f47efc1e18'
down_revision: Union[str, None] = '7694e3a5a6aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('health_checkups', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('health_checkups', schema=None) as batch_op:
        batch_op.drop_column('analysis')
