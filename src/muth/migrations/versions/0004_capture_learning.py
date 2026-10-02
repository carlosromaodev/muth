"""Encrypted, opt-in browser candidates awaiting independent review."""

import sqlalchemy as sa
from alembic import op

revision = "0004_capture_learning"
down_revision = "0003_capture"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "capture_learning_candidates",
        sa.Column("session_id", sa.String(48), primary_key=True),
        sa.Column("tenant_id", sa.String(80), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("retain_until", sa.Float(), nullable=False),
    )
    for field in ("tenant_id", "state", "retain_until"):
        op.create_index(
            f"ix_capture_learning_candidates_{field}", "capture_learning_candidates", [field]
        )


def downgrade():
    op.drop_table("capture_learning_candidates")
