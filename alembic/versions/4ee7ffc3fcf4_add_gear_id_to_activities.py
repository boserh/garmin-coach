"""Add gear_id to activities (NF-15 activity->gear link)

Revision ID: 4ee7ffc3fcf4
Revises: 723556154431
Create Date: 2026-08-01 13:00:22.546810
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4ee7ffc3fcf4'
down_revision: Union[str, None] = '723556154431'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gear_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('gear_id')
