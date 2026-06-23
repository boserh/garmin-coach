"""Add is_active to users

Revision ID: ceff8db3fa52
Revises: 1bd51bd9a080
Create Date: 2026-06-23 09:26:56.494448
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ceff8db3fa52'
down_revision: Union[str, None] = '1bd51bd9a080'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("is_active")
