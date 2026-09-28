"""Create content packages and formal entitlements."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        UTC_DATETIME,
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP(6)"),
    )


def upgrade() -> None:
    op.create_table(
        "content_package",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("internal_notes", sa.String(1000)),
        _created_at(),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','OFFLINE')", name="ck_content_package_status"
        ),
        sa.UniqueConstraint("public_id", name="uq_content_package_public_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "content_package_scene",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("package_id", BIGINT, nullable=False),
        sa.Column("scene_id", BIGINT, nullable=False),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(["package_id"], ["content_package.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"]),
        sa.UniqueConstraint("package_id", "scene_id", name="uq_package_scene"),
        sa.UniqueConstraint("package_id", "sort_order", name="uq_package_scene_order"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "formal_entitlement",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("package_id", BIGINT, nullable=False),
        sa.Column("term", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("granted_at", UTC_DATETIME, nullable=False),
        sa.Column("expires_at", UTC_DATETIME),
        sa.Column("version", BIGINT, nullable=False, server_default="1"),
        _created_at(),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.ForeignKeyConstraint(["package_id"], ["content_package.id"]),
        sa.CheckConstraint(
            "term IN ('MONTH_1','MONTH_2','MONTH_3','MONTH_6','MONTH_12','PERMANENT')",
            name="ck_formal_entitlement_term",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAUSED','REVOKED')",
            name="ck_formal_entitlement_status",
        ),
        sa.CheckConstraint(
            "(term = 'PERMANENT' AND expires_at IS NULL) OR "
            "(term <> 'PERMANENT' AND expires_at IS NOT NULL)",
            name="ck_formal_entitlement_expiry",
        ),
        sa.UniqueConstraint("public_id", name="uq_formal_entitlement_public_id"),
        sa.UniqueConstraint("user_id", "package_id", name="uq_formal_entitlement_current"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_formal_entitlement_expiry", "formal_entitlement", ["status", "expires_at"])
    op.create_table(
        "formal_entitlement_operation",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("entitlement_id", BIGINT, nullable=False),
        sa.Column("operation_type", sa.String(16), nullable=False),
        sa.Column("term", sa.String(16)),
        sa.Column("before_summary", mysql.JSON),
        sa.Column("after_summary", mysql.JSON, nullable=False),
        sa.Column("operator_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("result_entitlement_id", BIGINT, nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["entitlement_id"], ["formal_entitlement.id"]),
        sa.ForeignKeyConstraint(["result_entitlement_id"], ["formal_entitlement.id"]),
        sa.CheckConstraint(
            "operation_type IN ('GRANT','RENEW','PAUSE','RESUME','REVOKE')",
            name="ck_formal_operation_type",
        ),
        sa.UniqueConstraint("public_id", name="uq_formal_operation_public_id"),
        sa.UniqueConstraint(
            "operator_id", "idempotency_key", name="uq_formal_operation_idempotency"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute("UPDATE schema_version SET version = 4, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.execute("UPDATE schema_version SET version = 3, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_table("formal_entitlement_operation")
    op.drop_index("ix_formal_entitlement_expiry", table_name="formal_entitlement")
    op.drop_table("formal_entitlement")
    op.drop_table("content_package_scene")
    op.drop_table("content_package")
