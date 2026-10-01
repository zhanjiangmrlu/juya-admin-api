"""Merge backend integrity and operations defaults before exposing schema readiness."""

from alembic import op

revision = "0016"
down_revision = ("20261001_b_integrity", "20261001_ops_defaults")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE schema_version SET version=16 WHERE id=1")


def downgrade() -> None:
    op.execute("UPDATE schema_version SET version=15 WHERE id=1")
