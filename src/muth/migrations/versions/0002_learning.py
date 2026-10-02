"""Opt-in score feedback and versioned calibration registry."""

import sqlalchemy as sa
from alembic import op

revision = "0002_learning"
down_revision = "0001_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_samples",
        sa.Column("sample_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("session_id", sa.String(48), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("split", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("retain_until", sa.Float, nullable=False),
        sa.UniqueConstraint("tenant_id", "session_id", "role"),
    )
    for column in ("tenant_id", "session_id", "model_fingerprint"):
        op.create_index(f"ix_learning_samples_{column}", "learning_samples", [column])
    op.create_table(
        "calibration_versions",
        sa.Column("policy_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("model_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("test_panel_hash", sa.String(64), nullable=False),
        sa.Column("report", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    for column in ("tenant_id", "snapshot_hash"):
        op.create_index(f"ix_calibration_versions_{column}", "calibration_versions", [column])
    op.create_table(
        "active_calibrations",
        sa.Column("tenant_id", sa.String(80), primary_key=True),
        sa.Column("role", sa.String(16), primary_key=True),
        sa.Column("model_fingerprint", sa.String(64), primary_key=True),
        sa.Column("policy_id", sa.String(48)),
        sa.Column("generation", sa.Integer, nullable=False),
    )
    op.create_table(
        "calibration_members",
        sa.Column("policy_id", sa.String(48), primary_key=True),
        sa.Column("sample_id", sa.String(48), primary_key=True),
    )
    op.create_index("ix_calibration_members_sample_id", "calibration_members", ["sample_id"])


def downgrade():
    for name in (
        "calibration_members",
        "active_calibrations",
        "calibration_versions",
        "learning_samples",
    ):
        op.drop_table(name)
