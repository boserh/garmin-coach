"""Add garmin_creds_invalid to users

Revision ID: d0e4ab375720
Revises: cc5dca1a1c96
Create Date: 2026-08-03 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd0e4ab375720'
down_revision: Union[str, None] = 'cc5dca1a1c96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'garmin_creds_invalid', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('garmin_creds_invalid')
