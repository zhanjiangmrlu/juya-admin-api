"""Bind source comparisons to the chosen substantive fields, retaining full audit hashes."""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE content_work_issue_resolution ADD dependency_hash CHAR(64) NULL, "
        "ADD dependency_evidence JSON NULL"
    )
    op.execute("UPDATE schema_version SET version=23 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Preserve source comparison evidence; restore application only")
