"""Add feedback lifecycle tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "feedback_ticket",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source", mysql.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sla_hours", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("deadline_at", UTC_DATETIME),
        sa.Column("sla_remaining_seconds", mysql.INTEGER(unsigned=True)),
        sa.Column(
            "supplement_rounds", mysql.SMALLINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column(
            "reopen_count", mysql.SMALLINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("create_idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.Column("resolved_at", UTC_DATETIME),
        sa.Column("closed_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.UniqueConstraint("public_id", name="uq_feedback_ticket_public_id"),
        sa.UniqueConstraint(
            "user_id", "create_idempotency_key", name="uq_feedback_ticket_create_key"
        ),
        sa.CheckConstraint(
            "category IN ('CONTENT','PRONUNCIATION','DISPLAY','FUNCTION')",
            name="ck_feedback_ticket_category",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','NEED_MORE','USER_SUPPLIED',"
            "'RESOLVED','CLOSED_INSUFFICIENT')",
            name="ck_feedback_ticket_status",
        ),
        sa.CheckConstraint("supplement_rounds <= 2", name="ck_feedback_supplement_rounds"),
        sa.CheckConstraint("reopen_count <= 1", name="ck_feedback_reopen_count"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_feedback_ticket_status_deadline", "feedback_ticket", ["status", "deadline_at"]
    )
    op.create_index("ix_feedback_ticket_user", "feedback_ticket", ["user_id", "created_at"])

    op.create_table(
        "feedback_screenshot",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("security_status", sa.String(32), nullable=False),
        sa.Column("delete_after", UTC_DATETIME),
        sa.Column("deleted_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("ticket_id", name="uq_feedback_screenshot_ticket"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_feedback_screenshot_delete", "feedback_screenshot", ["delete_after", "deleted_at"]
    )

    op.create_table(
        "feedback_round",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("round_number", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("request_text", sa.String(200)),
        sa.Column("supplement_text", sa.String(300)),
        sa.Column("paused_at", UTC_DATETIME),
        sa.Column("supplied_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("ticket_id", "round_number", name="uq_feedback_round_number"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "feedback_reply",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("template", sa.String(64), nullable=False),
        sa.Column("note", sa.String(200)),
        sa.Column("admin_id", sa.CHAR(26), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("sent_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("ticket_id", "idempotency_key", name="uq_feedback_reply_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "feedback_timeline",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("payload", mysql.JSON, nullable=False),
        sa.Column("occurred_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_feedback_timeline_ticket_time", "feedback_timeline", ["ticket_id", "occurred_at"]
    )

    op.create_table(
        "feedback_command",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("command", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "ticket_id", "command", "idempotency_key", name="uq_feedback_command_key"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.execute("UPDATE schema_version SET version = 6, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    for table_name in (
        "feedback_command",
        "feedback_timeline",
        "feedback_reply",
        "feedback_round",
        "feedback_screenshot",
        "feedback_ticket",
    ):
        op.drop_table(table_name)
    op.execute("UPDATE schema_version SET version = 5, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
