"""Add administrative projections, anonymous analytics, and deletion cleanup."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.alter_column(
        "feedback_ticket",
        "user_id",
        existing_type=BIGINT,
        nullable=True,
    )
    op.create_table(
        "user_admin_projection",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("account_status", sa.String(32), nullable=False),
        sa.Column("last_active_at", UTC_DATETIME),
        sa.Column("formal_entitlement_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("limited_entitlement_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("open_feedback_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("projection_version", BIGINT, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_user_admin_projection_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_user_admin_projection_activity",
        "user_admin_projection",
        ["account_status", "last_active_at"],
    )
    op.create_table(
        "analytics_daily",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("metric_day", sa.Date(), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("dimension", sa.String(191), nullable=False),
        sa.Column("metric_value", BIGINT, nullable=False),
        sa.Column("generated_at", UTC_DATETIME, nullable=False),
        sa.UniqueConstraint("metric_day", "metric", "dimension", name="uq_analytics_daily_bucket"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "deletion_cleanup_event",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("user_public_id_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("screenshot_delete_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("completed_at", UTC_DATETIME),
        sa.UniqueConstraint("event_id", name="uq_deletion_cleanup_event_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.execute("UPDATE schema_version SET version = 8, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.drop_table("deletion_cleanup_event")
    op.drop_table("analytics_daily")
    op.drop_index("ix_user_admin_projection_activity", table_name="user_admin_projection")
    op.drop_table("user_admin_projection")
    op.alter_column(
        "feedback_ticket",
        "user_id",
        existing_type=BIGINT,
        nullable=False,
    )
    op.execute("UPDATE schema_version SET version = 7, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
