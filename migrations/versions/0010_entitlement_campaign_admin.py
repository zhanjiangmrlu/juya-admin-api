"""Add administration indexes, campaign revisions and command audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 功能:扩展权益活动管理索引和版本字段,新增活动命令审计表。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_constraint("ck_limited_campaign_status", "limited_campaign", type_="check")
    op.create_check_constraint(
        "ck_limited_campaign_status",
        "limited_campaign",
        "status IN ('DRAFT','OPEN','PAUSED','ENDED','ARCHIVED','CLOSED')",
    )
    op.drop_constraint("ck_campaign_version_status", "limited_campaign_version", type_="check")
    op.create_check_constraint(
        "ck_campaign_version_status",
        "limited_campaign_version",
        "status IN ('DRAFT','OPEN','PAUSED','ENDED','CLOSED')",
    )
    op.create_index("ix_formal_entitlement_admin", "formal_entitlement", ["granted_at", "id"])
    op.create_index("ix_limited_entitlement_admin", "limited_entitlement", ["granted_at", "id"])
    op.create_index("ix_content_package_admin", "content_package", ["status", "sort_order"])
    op.add_column(
        "limited_campaign",
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="1"),
    )
    op.add_column(
        "limited_campaign",
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
    )
    op.add_column(
        "limited_campaign_version",
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False, server_default="1"),
    )
    op.add_column(
        "limited_campaign_version",
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
    )
    op.create_index("ix_limited_campaign_admin", "limited_campaign", ["status", "created_at", "id"])
    op.create_table(
        "limited_campaign_operation",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("campaign_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("before_summary", mysql.JSON),
        sa.Column("after_summary", mysql.JSON, nullable=False),
        sa.Column("operator_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["limited_campaign.id"]),
        sa.UniqueConstraint("public_id", name="uq_limited_campaign_operation_public_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute("UPDATE schema_version SET version = 10, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:删除活动命令审计表并撤销新增管理字段与索引。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE limited_campaign SET status = 'CLOSED' WHERE status IN ('PAUSED','ENDED')")
    op.execute(
        "UPDATE limited_campaign_version SET status = 'CLOSED' WHERE status IN ('PAUSED','ENDED')"
    )
    op.drop_table("limited_campaign_operation")
    op.drop_index("ix_limited_campaign_admin", table_name="limited_campaign")
    op.drop_column("limited_campaign_version", "updated_at")
    op.drop_column("limited_campaign_version", "version")
    op.drop_column("limited_campaign", "updated_at")
    op.drop_column("limited_campaign", "version")
    op.drop_index("ix_content_package_admin", table_name="content_package")
    op.drop_index("ix_limited_entitlement_admin", table_name="limited_entitlement")
    op.drop_index("ix_formal_entitlement_admin", table_name="formal_entitlement")
    op.drop_constraint("ck_campaign_version_status", "limited_campaign_version", type_="check")
    op.create_check_constraint(
        "ck_campaign_version_status",
        "limited_campaign_version",
        "status IN ('DRAFT','OPEN','CLOSED')",
    )
    op.drop_constraint("ck_limited_campaign_status", "limited_campaign", type_="check")
    op.create_check_constraint(
        "ck_limited_campaign_status",
        "limited_campaign",
        "status IN ('DRAFT','OPEN','CLOSED','ARCHIVED')",
    )
    op.execute("UPDATE schema_version SET version = 9, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
