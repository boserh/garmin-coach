"""Add is_approved to users

Revision ID: 1bd51bd9a080
Revises: 5f1c2a9b7e44
Create Date: 2026-06-23 00:10:29.595856
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1bd51bd9a080'
down_revision: Union[str, None] = '5f1c2a9b7e44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing accounts are already active → backfill approved=True via server_default.
    with op.batch_alter_table("users") as b:
        b.add_column(
            sa.Column("is_approved", sa.Boolean(), nullable=False,
                      server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.drop_column("is_approved")
