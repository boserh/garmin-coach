"""add llm_cache table (PERF-02: dedup cache from JSON file to DB)

Revision ID: b7e4a9c1d2f3
Revises: a2b3c4d5e6f7
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e4a9c1d2f3'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'llm_cache',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index('ix_llm_cache_expires_at', 'llm_cache', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_llm_cache_expires_at', table_name='llm_cache')
    op.drop_table('llm_cache')
