"""Add administrative contact capabilities and canonical contact statuses."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 功能:扩展联系方式管理字段和索引,统一并回填旧联系方式状态。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_constraint("ck_user_contact_status", "user_contact", type_="check")
    op.execute(
        "UPDATE user_contact SET contact_status = 'CONTACTED' WHERE contact_status = 'VERIFIED'"
    )
    op.execute(
        "UPDATE user_contact SET contact_status = 'UNREACHABLE' WHERE contact_status = 'INVALID'"
    )
    op.create_check_constraint(
        "ck_user_contact_status",
        "user_contact",
        "contact_status IN ('NOT_PROVIDED','PENDING','CONTACTED','UNREACHABLE','DO_NOT_CONTACT')",
    )
    op.add_column(
        "contact_correction_request",
        sa.Column("processed_by", sa.CHAR(26)),
    )
    op.add_column(
        "contact_correction_request",
        sa.Column("decision_idempotency_key", sa.String(128)),
    )
    op.add_column(
        "contact_correction_request",
        sa.Column("decision_request_hash", sa.CHAR(64)),
    )
    op.create_unique_constraint(
        "uq_contact_correction_actor_idempotency",
        "contact_correction_request",
        ["processed_by", "decision_idempotency_key"],
    )
    op.create_index(
        "ix_contact_correction_status_created",
        "contact_correction_request",
        ["status", "created_at"],
    )
    op.execute("UPDATE schema_version SET version = 9, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:撤销联系方式管理新增字段和索引。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_index(
        "ix_contact_correction_status_created",
        table_name="contact_correction_request",
    )
    op.drop_constraint(
        "uq_contact_correction_actor_idempotency",
        "contact_correction_request",
        type_="unique",
    )
    op.drop_column("contact_correction_request", "decision_request_hash")
    op.drop_column("contact_correction_request", "decision_idempotency_key")
    op.drop_column("contact_correction_request", "processed_by")
    op.drop_constraint("ck_user_contact_status", "user_contact", type_="check")
    op.execute(
        "UPDATE user_contact SET contact_status = 'INVALID' "
        "WHERE contact_status IN ('UNREACHABLE','DO_NOT_CONTACT')"
    )
    op.create_check_constraint(
        "ck_user_contact_status",
        "user_contact",
        "contact_status IN ('NOT_PROVIDED','PENDING','CONTACTED','VERIFIED','INVALID')",
    )
    op.execute("UPDATE schema_version SET version = 8, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
