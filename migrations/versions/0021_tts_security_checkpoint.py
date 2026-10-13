"""Persist an independent Green task identity for paid TTS audio recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 独立保存音频审核任务和提交状态,保留已有腾讯请求标识与付费音频
    op.add_column("pronunciation_task", sa.Column("security_request_id", sa.String(128)))
    op.add_column("pronunciation_task", sa.Column("security_status", sa.String(32)))
    op.execute("UPDATE schema_version SET version=21 WHERE id=1")


def downgrade() -> None:
    # 审核任务标识属于恢复证据,不能因应用回退而丢失并触发重复提交
    raise RuntimeError("Retain paid audio moderation task identities; restore the application only")
