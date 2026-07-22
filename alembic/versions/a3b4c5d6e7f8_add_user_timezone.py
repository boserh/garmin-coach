"""Add timezone to users (ST-14 per-user timezone)

Revision ID: a3b4c5d6e7f8
Revises: e48815b991f2
Create Date: 2026-07-22 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'e48815b991f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'timezone', sa.String(length=64), nullable=False,
            server_default='Europe/Warsaw'))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('timezone')
