"""Add routes table + activities.route_id (NF-33)

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a8b9
Create Date: 2026-08-05 21:50:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c3d4e5f6a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'routes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('fingerprint', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_routes_user_id', 'routes', ['user_id'])
    op.create_index('ix_routes_user_created', 'routes', ['user_id', 'created_at'])
    with op.batch_alter_table('activities') as batch:
        batch.add_column(sa.Column('route_id', sa.Integer(), nullable=True))
        batch.create_index('ix_activities_route_id', ['route_id'])
        batch.create_foreign_key('fk_activities_route', 'routes', ['route_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('activities') as batch:
        batch.drop_constraint('fk_activities_route', type_='foreignkey')
        batch.drop_index('ix_activities_route_id')
        batch.drop_column('route_id')
    op.drop_index('ix_routes_user_created', table_name='routes')
    op.drop_index('ix_routes_user_id', table_name='routes')
    op.drop_table('routes')
