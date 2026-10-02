"""Encrypted browser capture reports alongside existing verification results."""

import sqlalchemy as sa
from alembic import op

revision = "0003_capture"
down_revision = "0002_learning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("verification_sessions", sa.Column("capture_result", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("verification_sessions") as batch:
        batch.drop_column("capture_result")
