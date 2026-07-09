"""Add subjective check-in (RPE + pain) to activities (EP-12)

Revision ID: c1d2e3f4a5b6
Revises: b7e4a9c1d2f3
Create Date: 2026-07-09 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b7e4a9c1d2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subjective', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('subjective')
