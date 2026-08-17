"""NF-34: away periods (vacation/trip context the coach reads)

Revision ID: c7d8e9f0a1b2
Revises: 343cc3f80d00
Create Date: 2026-08-16 10:00:00.000000

A declared stretch of days away from normal training, plus what the athlete will be doing
instead (kind + free-text note). Without it a planned week off and a collapsed week are the
same zero in the data, and the weekly digest scored a vacation as "ні, відстаєш".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = '343cc3f80d00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'away_periods',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('start_date', sa.String(length=10), nullable=False),
        sa.Column('end_date', sa.String(length=10), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='rest'),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_away_periods_user_id', 'away_periods', ['user_id'])
    op.create_index('ix_away_user_start', 'away_periods', ['user_id', 'start_date'])


def downgrade() -> None:
    op.drop_index('ix_away_user_start', table_name='away_periods')
    op.drop_index('ix_away_periods_user_id', table_name='away_periods')
    op.drop_table('away_periods')
