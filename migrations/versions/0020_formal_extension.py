"""Allow a distinct auditable formal entitlement extension operation."""

import sqlalchemy as sa
from alembic import context, op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 扩展正式权益审计动作约束,保留已有操作记录并提升所需数据库版本
    op.drop_constraint("ck_formal_operation_type", "formal_entitlement_operation", type_="check")
    op.create_check_constraint(
        "ck_formal_operation_type",
        "formal_entitlement_operation",
        "operation_type IN ('GRANT','RENEW','EXTEND','PAUSE','RESUME','REVOKE')",
    )
    op.execute("UPDATE schema_version SET version=20 WHERE id=1")


def downgrade() -> None:
    # 已有延期审计记录不能改写或丢失,回退前必须由运营明确处理该兼容边界
    if context.is_offline_mode() or op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM formal_entitlement_operation WHERE operation_type='EXTEND'")
    ):
        raise RuntimeError("Cannot downgrade while formal extension audit records exist")
    op.drop_constraint("ck_formal_operation_type", "formal_entitlement_operation", type_="check")
    op.create_check_constraint(
        "ck_formal_operation_type",
        "formal_entitlement_operation",
        "operation_type IN ('GRANT','RENEW','PAUSE','RESUME','REVOKE')",
    )
    op.execute("UPDATE schema_version SET version=19 WHERE id=1")
