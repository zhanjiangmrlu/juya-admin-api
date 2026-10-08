"""Backend integrity: recoverable batches, atomic effects and validated review cards."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "20261001_b_integrity"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 功能:增加可恢复批次及原子操作收据,强化复习卡片和联系方式完整性。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.add_column("batch_job", sa.Column("lease_token", sa.String(26), nullable=True))
    op.add_column("batch_job", sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True))
    op.create_index("ix_batch_recovery", "batch_job", ["status", "lease_expires_at"])
    op.create_table(
        "batch_operation_receipt",
        sa.Column("operation_key", sa.String(191), primary_key=True),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("result_payload", mysql.JSON, nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
    )
    op.add_column("review_session", sa.Column("card_ids", mysql.JSON, nullable=True))


def downgrade() -> None:
    # 功能:撤销批操作收据和本版本新增的恢复与完整性约束。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_column("review_session", "card_ids")
    op.drop_table("batch_operation_receipt")
    op.drop_index("ix_batch_recovery", table_name="batch_job")
    op.drop_column("batch_job", "lease_expires_at")
    op.drop_column("batch_job", "lease_token")
