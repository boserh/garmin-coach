"""Add personal_records table (EP-14)

Revision ID: e7f8a9b0c1d2
Revises: c1d2e3f4a5b6
Create Date: 2026-07-11 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'personal_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('previous_value', sa.Float(), nullable=True),
        sa.Column('activity_id', sa.Integer(), nullable=True),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['activity_id'], ['activities.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_personal_records_user_id', 'personal_records', ['user_id'])
    op.create_index('ix_personal_records_kind', 'personal_records', ['kind'])
    op.create_index('ix_personal_records_date', 'personal_records', ['date'])


def downgrade() -> None:
    op.drop_index('ix_personal_records_date', table_name='personal_records')
    op.drop_index('ix_personal_records_kind', table_name='personal_records')
    op.drop_index('ix_personal_records_user_id', table_name='personal_records')
    op.drop_table('personal_records')
