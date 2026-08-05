"""Add lifestyle_logs table (NF-28)

Revision ID: a1b2c3d4e5f7
Revises: 6ec6656686f2
Create Date: 2026-08-05 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = '6ec6656686f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lifestyle_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('note', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'date', name='uq_lifestyle_user_date'),
    )
    op.create_index('ix_lifestyle_logs_user_id', 'lifestyle_logs', ['user_id'])
    op.create_index('ix_lifestyle_logs_date', 'lifestyle_logs', ['date'])


def downgrade() -> None:
    op.drop_index('ix_lifestyle_logs_date', table_name='lifestyle_logs')
    op.drop_index('ix_lifestyle_logs_user_id', table_name='lifestyle_logs')
    op.drop_table('lifestyle_logs')
