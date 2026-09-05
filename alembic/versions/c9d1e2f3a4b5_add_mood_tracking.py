"""Add NF-35 daytime mood tracking (users.mood_tracking_enabled + lifestyle_logs columns)

Revision ID: a1b2c3d4e5f6
Revises: e1f2a3b4c5d6
Create Date: 2026-09-05 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d1e2f3a4b5'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column(
            'mood_tracking_enabled', sa.Boolean(), nullable=False,
            server_default=sa.false()))
    with op.batch_alter_table('lifestyle_logs') as batch:
        batch.add_column(sa.Column('energy_level', sa.String(16), nullable=True))
        batch.add_column(sa.Column('mood', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('irritability', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lifestyle_logs') as batch:
        batch.drop_column('irritability')
        batch.drop_column('mood')
        batch.drop_column('energy_level')
    with op.batch_alter_table('users') as batch:
        batch.drop_column('mood_tracking_enabled')
