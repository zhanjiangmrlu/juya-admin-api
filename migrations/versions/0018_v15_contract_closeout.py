"""Add short-reading operations order and durable explicit publication ranges."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "short_reading", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_index("ix_short_reading_order", "short_reading", ["sort_order", "public_id"])
    op.add_column(
        "content_cloud_attempt",
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_index("uq_content_cloud_attempt_stage", table_name="content_cloud_attempt")
    op.create_unique_constraint(
        "uq_content_cloud_attempt_stage_attempt",
        "content_cloud_attempt",
        ["stage_id", "attempt_number"],
    )
    op.create_table(
        "publication_range",
        sa.Column("public_id", sa.String(64, collation="utf8mb4_bin"), primary_key=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("collection_hash", sa.CHAR(64), nullable=False),
        sa.Column("candidates", mysql.JSON(), nullable=False),
        sa.Column("results", mysql.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="'PREVIEW'"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stage_id", sa.String(64)),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.execute("UPDATE schema_version SET version=18 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Retain durable publication receipts and immutable snapshots")
