"""Add tool_rounds to report_logs (EP-09 /ask tool-use round count)

Revision ID: f19ea8917ca6
Revises: b1c2d3e4f5a6
Create Date: 2026-07-18 13:24:46.451088
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f19ea8917ca6'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('report_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tool_rounds', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('report_logs', schema=None) as batch_op:
        batch_op.drop_column('tool_rounds')
