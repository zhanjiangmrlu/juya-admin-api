"""Add feedback administration notes and query indexes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    # 功能:新增反馈内部备注表及管理查询索引。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.create_index(
        "ix_feedback_ticket_category_status_created",
        "feedback_ticket",
        ["category", "status", "created_at", "id"],
    )
    op.create_table(
        "feedback_internal_note",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("ticket_id", BIGINT, nullable=False),
        sa.Column("admin_id", sa.String(64), nullable=False),
        sa.Column("content", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["feedback_ticket.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("public_id", name="uq_feedback_internal_note_public_id"),
        sa.UniqueConstraint(
            "ticket_id",
            "admin_id",
            "idempotency_key",
            name="uq_feedback_internal_note_idempotency",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_feedback_internal_note_ticket_created",
        "feedback_internal_note",
        ["ticket_id", "created_at", "id"],
    )
    op.execute("UPDATE schema_version SET version = 11, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:删除内部备注表并撤销反馈管理索引。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_table("feedback_internal_note")
    op.drop_index(
        "ix_feedback_ticket_category_status_created",
        table_name="feedback_ticket",
    )
    op.execute("UPDATE schema_version SET version = 10, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
