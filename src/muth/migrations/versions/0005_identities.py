"""Separate enrolment consent and encrypted, provisional identity profiles."""

import sqlalchemy as sa
from alembic import op

revision = "0005_identities"
down_revision = "0004_capture_learning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "verification_sessions", sa.Column("enrollment_consent", sa.Text(), nullable=True)
    )
    op.create_table(
        "identity_profiles",
        sa.Column("identity_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("source_session_id", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("retain_until", sa.Float(), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_session_id"),
    )
    for field in ("tenant_id", "source_session_id", "retain_until"):
        op.create_index(f"ix_identity_profiles_{field}", "identity_profiles", [field])


def downgrade():
    op.drop_table("identity_profiles")
    with op.batch_alter_table("verification_sessions") as batch:
        batch.drop_column("enrollment_consent")
