"""mcp oauth server tables (NF-08 http transport)

Autogenerate also reported a pile of unrelated drift between the models and older
migrations (missing FK constraints on user_id columns, NOT NULL on a few timestamps).
None of it belongs to this change and on SQLite each one is a full table rebuild, so it
was stripped: this revision creates the two new tables and nothing else.

Revision ID: 7a2b1e7cbd53
Revises: d5e6f7a8b9c0
Create Date: 2026-08-07 16:55:15.517005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7a2b1e7cbd53'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'oauth_clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('data_enc', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_oauth_clients_client_id'), 'oauth_clients', ['client_id'], unique=True
    )

    op.create_table(
        'oauth_grants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('scopes', sa.JSON(), nullable=True),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_oauth_grants_client_id'), 'oauth_grants', ['client_id'], unique=False
    )
    op.create_index(op.f('ix_oauth_grants_kind'), 'oauth_grants', ['kind'], unique=False)
    op.create_index(
        'ix_oauth_grants_kind_expires', 'oauth_grants', ['kind', 'expires_at'], unique=False
    )
    op.create_index(
        op.f('ix_oauth_grants_token_hash'), 'oauth_grants', ['token_hash'], unique=True
    )
    op.create_index(
        op.f('ix_oauth_grants_user_id'), 'oauth_grants', ['user_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_oauth_grants_user_id'), table_name='oauth_grants')
    op.drop_index(op.f('ix_oauth_grants_token_hash'), table_name='oauth_grants')
    op.drop_index('ix_oauth_grants_kind_expires', table_name='oauth_grants')
    op.drop_index(op.f('ix_oauth_grants_kind'), table_name='oauth_grants')
    op.drop_index(op.f('ix_oauth_grants_client_id'), table_name='oauth_grants')
    op.drop_table('oauth_grants')

    op.drop_index(op.f('ix_oauth_clients_client_id'), table_name='oauth_clients')
    op.drop_table('oauth_clients')
