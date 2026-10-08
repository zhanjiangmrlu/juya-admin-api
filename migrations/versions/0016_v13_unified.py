"""Merge backend integrity and operations defaults before exposing schema readiness."""

from alembic import op

revision = "0016"
down_revision = ("20261001_b_integrity", "20261001_ops_defaults")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 功能:合并完整性和运营默认值迁移分支,更新服务就绪所需版本标记。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE schema_version SET version=16 WHERE id=1")


def downgrade() -> None:
    # 功能:撤销统一版本标记,恢复合并前的迁移版本状态。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE schema_version SET version=15 WHERE id=1")
