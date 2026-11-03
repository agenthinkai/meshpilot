"""add privacy policy, audit trail and fit report tables

Revision ID: d7e4a91c2f50
Revises: c3d81f0a6b27
Create Date: 2026-11-03 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e4a91c2f50'
down_revision: Union[str, None] = 'c3d81f0a6b27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('privacy_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cloud_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('on_sensitive', sa.String(length=16), nullable=False, server_default='local'),
    sa.Column('blocked_terms', sa.JSON(), nullable=False, server_default='[]'),
    sa.Column('auto_cloud_min_chars', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_by', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint('id = 1', name='ck_privacy_policy_single_row'),
    )
    op.execute("INSERT INTO privacy_policies (id) VALUES (1)")

    op.create_table('audit_events',
    sa.Column('seq', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('actor_user_id', sa.String(length=36), nullable=True),
    sa.Column('actor_email', sa.String(length=255), nullable=True),
    sa.Column('actor_key_prefix', sa.String(length=16), nullable=True),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('model_id', sa.String(length=64), nullable=True),
    sa.Column('route', sa.String(length=16), nullable=True),
    sa.Column('destination', sa.String(length=255), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('sensitive', sa.JSON(), nullable=True),
    sa.Column('prompt_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('seq'),
    )
    op.create_index('ix_audit_created', 'audit_events', ['created_at'], unique=False)
    op.create_index('ix_audit_actor', 'audit_events', ['actor_user_id'], unique=False)
    op.create_index('ix_audit_route', 'audit_events', ['route'], unique=False)

    # The application can only ever add audit rows.
    op.execute("""
        CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER audit_events_no_change BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()
    """)
    op.execute("""
        CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()
    """)

    op.create_table('fit_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=True),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('suite', sa.JSON(), nullable=False),
    sa.Column('model_ids', sa.JSON(), nullable=False),
    sa.Column('progress', sa.JSON(), nullable=True),
    sa.Column('report', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_fit_run_user', 'fit_runs', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_fit_run_user', table_name='fit_runs')
    op.drop_table('fit_runs')
    op.execute("DROP TABLE audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
    op.drop_table('privacy_policies')
