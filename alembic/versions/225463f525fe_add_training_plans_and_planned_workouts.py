"""Add training_plans and planned_workouts

Revision ID: 225463f525fe
Revises: 119be2b423ed
Create Date: 2026-06-25 18:12:52.032522
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '225463f525fe'
down_revision: Union[str, None] = '119be2b423ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'training_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('goal', sa.String(length=32), nullable=False),
        sa.Column('goal_label', sa.String(length=128), nullable=True),
        sa.Column('target_date', sa.String(length=10), nullable=True),
        sa.Column('start_date', sa.String(length=10), nullable=True),
        sa.Column('days_per_week', sa.Integer(), nullable=True),
        sa.Column('intensity', sa.String(length=16), nullable=True),
        sa.Column('intake', sa.JSON(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('training_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_training_plans_user_id'), ['user_id'], unique=False)

    op.create_table(
        'planned_workouts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('date', sa.String(length=10), nullable=False),
        sa.Column('week', sa.Integer(), nullable=True),
        sa.Column('type', sa.String(length=16), nullable=True),
        sa.Column('dist_km', sa.Float(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['training_plans.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_planned_workouts_date'), ['date'], unique=False)
        batch_op.create_index(batch_op.f('ix_planned_workouts_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_planned_workouts_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('planned_workouts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_planned_workouts_user_id'))
        batch_op.drop_index(batch_op.f('ix_planned_workouts_plan_id'))
        batch_op.drop_index(batch_op.f('ix_planned_workouts_date'))
    op.drop_table('planned_workouts')
    with op.batch_alter_table('training_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_training_plans_user_id'))
    op.drop_table('training_plans')
