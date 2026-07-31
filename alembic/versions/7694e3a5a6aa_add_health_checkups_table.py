"""Add health_checkups table

Revision ID: 7694e3a5a6aa
Revises: c5d6e7f8a9b0
Create Date: 2026-07-30 22:23:57.089655
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7694e3a5a6aa'
down_revision: Union[str, None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'health_checkups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=True),
        sa.Column('results', sa.JSON(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('next_due_date', sa.String(length=10), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('health_checkups', schema=None) as batch_op:
        batch_op.create_index('ix_health_checkups_user_date', ['user_id', 'date'], unique=False)
        batch_op.create_index(batch_op.f('ix_health_checkups_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('health_checkups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_health_checkups_user_id'))
        batch_op.drop_index('ix_health_checkups_user_date')

    op.drop_table('health_checkups')
