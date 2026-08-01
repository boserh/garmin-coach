"""Add checkup_attachments table

Revision ID: cc5dca1a1c96
Revises: 4ee7ffc3fcf4
Create Date: 2026-08-01 17:40:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cc5dca1a1c96'
down_revision: Union[str, None] = '4ee7ffc3fcf4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'checkup_attachments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('checkup_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('media_type', sa.String(length=64), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['checkup_id'], ['health_checkups.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('checkup_attachments', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_checkup_attachments_checkup_id'), ['checkup_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('checkup_attachments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_checkup_attachments_checkup_id'))

    op.drop_table('checkup_attachments')
