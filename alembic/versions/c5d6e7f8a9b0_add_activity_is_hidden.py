"""Add is_hidden to activities (ST-17 hide activity)

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-07-24 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_hidden', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('is_hidden')
