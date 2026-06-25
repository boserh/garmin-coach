"""Add series column to activities

Revision ID: cd75b91e2ea9
Revises: 64c93b24ed23
Create Date: 2026-06-25 09:14:22.656165
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cd75b91e2ea9'
down_revision: Union[str, None] = '64c93b24ed23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('series', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('series')
