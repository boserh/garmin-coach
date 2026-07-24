"""Add job_runs table (OPS-04 background-job run log)

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-07-24 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('detail', sa.String(length=512), nullable=True),
        sa.Column('count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('run_date', sa.String(length=10), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_job_runs_job', 'job_runs', ['job'])
    op.create_index('ix_job_runs_user_id', 'job_runs', ['user_id'])
    op.create_index('ix_job_runs_user_started', 'job_runs', ['user_id', 'started_at'])
    op.create_index('ix_job_runs_job_started', 'job_runs', ['job', 'started_at'])


def downgrade() -> None:
    op.drop_index('ix_job_runs_job_started', table_name='job_runs')
    op.drop_index('ix_job_runs_user_started', table_name='job_runs')
    op.drop_index('ix_job_runs_user_id', table_name='job_runs')
    op.drop_index('ix_job_runs_job', table_name='job_runs')
    op.drop_table('job_runs')
