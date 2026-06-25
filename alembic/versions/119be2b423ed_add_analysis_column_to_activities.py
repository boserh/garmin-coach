"""Add analysis column to activities

Revision ID: 119be2b423ed
Revises: cd75b91e2ea9
Create Date: 2026-06-25 13:19:33.231745
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '119be2b423ed'
down_revision: Union[str, None] = 'cd75b91e2ea9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('activities', schema=None) as batch_op:
        batch_op.drop_column('analysis')
