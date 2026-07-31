"""Add supplements table

Revision ID: 723556154431
Revises: d9f47efc1e18
Create Date: 2026-07-31 06:39:13.366014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '723556154431'
down_revision: Union[str, None] = 'd9f47efc1e18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'supplements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('dosage', sa.String(length=64), nullable=True),
        sa.Column('frequency', sa.String(length=64), nullable=True),
        sa.Column('started_date', sa.String(length=10), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('supplements', schema=None) as batch_op:
        batch_op.create_index('ix_supplements_user_active', ['user_id', 'is_active'], unique=False)
        batch_op.create_index(batch_op.f('ix_supplements_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('supplements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_supplements_user_id'))
        batch_op.drop_index('ix_supplements_user_active')

    op.drop_table('supplements')
