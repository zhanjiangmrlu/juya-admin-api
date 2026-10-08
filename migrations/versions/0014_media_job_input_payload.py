"""Persist replay-safe media job input payloads."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 功能:为媒体任务增加可重放的输入载荷字段。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.add_column("processing_job", sa.Column("input_payload", mysql.JSON))
    op.execute("UPDATE processing_job SET input_payload = JSON_OBJECT()")
    op.alter_column(
        "processing_job",
        "input_payload",
        existing_type=mysql.JSON,
        nullable=False,
    )
    op.execute("UPDATE schema_version SET version = 14, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:删除媒体任务的输入载荷字段。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE schema_version SET version = 13, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_column("processing_job", "input_payload")
