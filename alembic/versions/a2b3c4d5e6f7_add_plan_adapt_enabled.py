"""Add plan_adapt_enabled to users; widen bot_state.value to Text

Revision ID: a2b3c4d5e6f7
Revises: 1efa5d6615f3
Create Date: 2026-07-04 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = '1efa5d6615f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'plan_adapt_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    # bot_state now also stores pending plan-adaptation proposals (serialized ops JSON,
    # can exceed 256 chars) — widen the value column.
    with op.batch_alter_table('bot_state', schema=None) as batch_op:
        batch_op.alter_column('value', existing_type=sa.String(256), type_=sa.Text())


def downgrade() -> None:
    with op.batch_alter_table('bot_state', schema=None) as batch_op:
        batch_op.alter_column('value', existing_type=sa.Text(), type_=sa.String(256))
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('plan_adapt_enabled')
