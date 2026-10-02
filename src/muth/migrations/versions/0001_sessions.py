"""Persistent sessions and minimal tenant-scoped audit events."""

import sqlalchemy as sa
from alembic import op

revision = "0001_sessions"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "verification_sessions",
        sa.Column("session_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("retain_until", sa.Float, nullable=False),
        sa.Column("payload", sa.Text),
        sa.Column("result", sa.Text),
        sa.Column("idempotency_hash", sa.String(64)),
        sa.Column("fingerprint", sa.String(64)),
        sa.Column("attempt", sa.String(32)),
        sa.Column("claim_until", sa.Float),
    )
    op.create_index("ix_verification_sessions_tenant_id", "verification_sessions", ["tenant_id"])
    op.create_index(
        "ix_verification_sessions_retain_until", "verification_sessions", ["retain_until"]
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("session_id", sa.String(48), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_session_id", "audit_events", ["session_id"])


def downgrade():
    op.drop_table("audit_events")
    op.drop_table("verification_sessions")
