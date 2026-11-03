"""add compare_runs table

Revision ID: c3d81f0a6b27
Revises: 92c907b2c75a
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d81f0a6b27'
down_revision: Union[str, None] = '92c907b2c75a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('compare_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=True),
    sa.Column('model_id', sa.String(length=36), nullable=False),
    sa.Column('mesh_prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('mesh_completion_tokens', sa.Integer(), nullable=False),
    sa.Column('mesh_latency_ms', sa.Float(), nullable=False),
    sa.Column('mesh_memory_mb', sa.Float(), nullable=True),
    sa.Column('baseline_prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('baseline_completion_tokens', sa.Integer(), nullable=False),
    sa.Column('baseline_latency_ms', sa.Float(), nullable=False),
    sa.Column('baseline_memory_mb', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['model_id'], ['model_records.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_compare_run_user', 'compare_runs', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_compare_run_user', table_name='compare_runs')
    op.drop_table('compare_runs')
