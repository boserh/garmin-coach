"""Add activities.start_lat/start_lon (travel-aware weather location)

Revision ID: e1f2a3b4c5d6
Revises: d8e9f0a1b2c3
Create Date: 2026-08-21 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities') as batch:
        batch.add_column(sa.Column('start_lat', sa.Float(), nullable=True))
        batch.add_column(sa.Column('start_lon', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities') as batch:
        batch.drop_column('start_lon')
        batch.drop_column('start_lat')
