"""Add question column to report_logs

Revision ID: 64c93b24ed23
Revises: ceff8db3fa52
Create Date: 2026-06-24 00:15:42.845294
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '64c93b24ed23'
down_revision: Union[str, None] = 'ceff8db3fa52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('report_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('question', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('report_logs', schema=None) as batch_op:
        batch_op.drop_column('question')
