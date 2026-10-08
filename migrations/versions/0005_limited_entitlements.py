"""Create limited campaigns, snapshots, entitlements, and operations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def _created_at() -> sa.Column[object]:
    # 功能:构造带默认当前时间的 created_at 迁移列。
    # 参数:无。
    # 返回:带当前时间默认值的 SQLAlchemy 时间列。
    return sa.Column(
        "created_at",
        UTC_DATETIME,
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP(6)"),
    )


def upgrade() -> None:
    # 功能:创建限时活动、版本快照、场景关联及限时权益操作表。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.create_table(
        "limited_campaign",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("current_version_id", BIGINT),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('DRAFT','OPEN','CLOSED','ARCHIVED')",
            name="ck_limited_campaign_status",
        ),
        sa.UniqueConstraint("public_id", name="uq_limited_campaign_public_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "limited_campaign_version",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("campaign_id", BIGINT, nullable=False),
        sa.Column("version_no", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("duration_days", mysql.TINYINT(unsigned=True), nullable=False),
        sa.Column("activation_window_days", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("capacity", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column(
            "granted_user_count",
            mysql.INTEGER(unsigned=True),
            nullable=False,
            server_default="0",
        ),
        sa.Column("grant_starts_at", UTC_DATETIME),
        sa.Column("grant_ends_at", UTC_DATETIME),
        sa.Column("locked_at", UTC_DATETIME),
        _created_at(),
        sa.ForeignKeyConstraint(["campaign_id"], ["limited_campaign.id"]),
        sa.CheckConstraint("duration_days IN (3, 5)", name="ck_campaign_duration"),
        sa.CheckConstraint("capacity >= granted_user_count", name="ck_campaign_capacity"),
        sa.CheckConstraint(
            "status IN ('DRAFT','OPEN','CLOSED')", name="ck_campaign_version_status"
        ),
        sa.UniqueConstraint("public_id", name="uq_campaign_version_public_id"),
        sa.UniqueConstraint("campaign_id", "version_no", name="uq_campaign_version_no"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_foreign_key(
        "fk_limited_campaign_current_version",
        "limited_campaign",
        "limited_campaign_version",
        ["current_version_id"],
        ["id"],
    )
    op.create_table(
        "limited_campaign_scene",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("campaign_version_id", BIGINT, nullable=False),
        sa.Column("scene_id", BIGINT, nullable=False),
        sa.Column("position", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("scene_revision_id", BIGINT, nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_version_id"], ["limited_campaign_version.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"]),
        sa.ForeignKeyConstraint(["scene_revision_id"], ["scene_revision.id"]),
        sa.UniqueConstraint("campaign_version_id", "scene_id", name="uq_campaign_version_scene"),
        sa.UniqueConstraint(
            "campaign_version_id", "position", name="uq_campaign_version_scene_order"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "limited_entitlement",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("campaign_version_id", BIGINT, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("granted_at", UTC_DATETIME, nullable=False),
        sa.Column("start_deadline", UTC_DATETIME, nullable=False),
        sa.Column("activated_at", UTC_DATETIME),
        sa.Column("expires_at", UTC_DATETIME),
        sa.Column("remedy_count", mysql.TINYINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("version", BIGINT, nullable=False, server_default="1"),
        _created_at(),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["campaign_version_id"], ["limited_campaign_version.id"]),
        sa.CheckConstraint(
            "status IN ('PENDING','ACTIVE','PAUSED','ENDED','START_EXPIRED','REVOKED')",
            name="ck_limited_entitlement_status",
        ),
        sa.CheckConstraint("remedy_count <= 1", name="ck_limited_entitlement_remedy"),
        sa.UniqueConstraint("public_id", name="uq_limited_entitlement_public_id"),
        sa.UniqueConstraint(
            "user_id", "campaign_version_id", name="uq_limited_entitlement_user_version"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_limited_entitlement_expiry",
        "limited_entitlement",
        ["status", "expires_at", "start_deadline"],
    )
    op.create_table(
        "limited_entitlement_operation",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("entitlement_id", BIGINT, nullable=False),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("before_summary", mysql.JSON),
        sa.Column("after_summary", mysql.JSON, nullable=False),
        sa.Column("operator_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("result_entitlement_id", BIGINT, nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["entitlement_id"], ["limited_entitlement.id"]),
        sa.ForeignKeyConstraint(["result_entitlement_id"], ["limited_entitlement.id"]),
        sa.UniqueConstraint("public_id", name="uq_limited_operation_public_id"),
        sa.UniqueConstraint(
            "operator_id", "idempotency_key", name="uq_limited_operation_idempotency"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute("UPDATE schema_version SET version = 5, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:删除限时权益、活动版本及相关操作表。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE schema_version SET version = 4, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_table("limited_entitlement_operation")
    op.drop_index("ix_limited_entitlement_expiry", table_name="limited_entitlement")
    op.drop_table("limited_entitlement")
    op.drop_table("limited_campaign_scene")
    op.drop_constraint(
        "fk_limited_campaign_current_version", "limited_campaign", type_="foreignkey"
    )
    op.drop_table("limited_campaign_version")
    op.drop_table("limited_campaign")
