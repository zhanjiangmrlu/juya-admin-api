"""Version-pinned content, curated lexicon, OCR reservations and anonymous events."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
BIGINT = mysql.BIGINT(unsigned=True)
DATE = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    # 功能:新增版本固定的内容词库、资源引用、OCR 额度及匿名事件存储。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.alter_column(
        "scene_dialogue_sentence",
        "stable_id",
        type_=sa.String(64),
        existing_type=sa.CHAR(26),
        existing_nullable=False,
    )
    op.alter_column(
        "scene_entry_source",
        "sentence_stable_id",
        type_=sa.String(64),
        existing_type=sa.CHAR(26),
        existing_nullable=True,
    )
    for name in ("width", "height", "duration_ms"):
        op.add_column("media_asset", sa.Column(name, BIGINT, nullable=True))
    op.add_column("media_asset", sa.Column("security_request_id", sa.String(128), nullable=True))
    op.add_column("content_series", sa.Column("cover_asset_id", sa.CHAR(26)))
    op.add_column("batch_job", sa.Column("input_payload", mysql.JSON))
    op.add_column("batch_job", sa.Column("result_payload", mysql.JSON))
    op.create_table(
        "lexicon_entry",
        sa.Column("public_id", sa.CHAR(26), primary_key=True),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("normalized_english", sa.String(500), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", DATE, nullable=False),
        sa.UniqueConstraint("entry_type", "normalized_english", name="uq_lexicon_type_english"),
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "lexicon_entry_version",
        sa.Column("entry_id", sa.CHAR(26), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("content", mysql.JSON, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", DATE, nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["lexicon_entry.public_id"]),
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "scene_media_reference",
        sa.Column("revision_id", BIGINT, primary_key=True),
        sa.Column("asset_id", BIGINT, primary_key=True),
        sa.ForeignKeyConstraint(["revision_id"], ["scene_revision.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.id"]),
    )
    op.create_table(
        "ocr_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("monthly_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("free_quota", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_disabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("quota_verified_at", DATE),
        sa.Column("updated_by", sa.String(64)),
        sa.Column("updated_at", DATE),
    )
    op.execute("INSERT INTO ocr_settings (id) VALUES (1)")
    op.create_table(
        "ocr_monthly_usage",
        sa.Column("month", sa.CHAR(7), primary_key=True),
        sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "ocr_quota_reservation",
        sa.Column("job_public_id", sa.CHAR(26), primary_key=True),
        sa.Column("month", sa.CHAR(7), nullable=False),
        sa.Column("created_at", DATE, nullable=False),
        sa.ForeignKeyConstraint(["month"], ["ocr_monthly_usage.month"]),
    )
    op.create_table(
        "analytics_event",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("event_key", sa.String(191), nullable=False),
        sa.Column("user_id", BIGINT),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("occurred_at", DATE, nullable=False),
        sa.Column("dimension", sa.String(191), nullable=False, server_default="ALL"),
        sa.Column("payload", mysql.JSON, nullable=False),
        sa.UniqueConstraint("event_key", name="uq_analytics_event_key"),
        sa.Index("ix_analytics_event_time_type", "occurred_at", "event_type"),
    )
    op.add_column("favorite_source", sa.Column("revision_id", sa.CHAR(26)))
    op.add_column("favorite_source", sa.Column("entry_version", sa.Integer(), server_default="1"))
    op.add_column("favorite_source", sa.Column("entry_snapshot", mysql.JSON))
    op.drop_constraint("ck_formal_entitlement_term", "formal_entitlement", type_="check")
    op.execute("UPDATE formal_entitlement SET term = LOWER(REPLACE(term, ' ', ''))")
    op.create_check_constraint(
        "ck_formal_entitlement_term",
        "formal_entitlement",
        "BINARY term IN ('month_1','month_2','month_3','month_6','month_12','permanent')",
    )
    op.execute("UPDATE schema_version SET version=15, updated_at=UTC_TIMESTAMP(6) WHERE id=1")


def downgrade() -> None:
    # 功能:删除 V1.3 内容生产新增表并撤销对应字段及约束。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.drop_constraint("ck_formal_entitlement_term", "formal_entitlement", type_="check")
    op.execute("UPDATE formal_entitlement SET term=UPPER(term)")
    op.create_check_constraint(
        "ck_formal_entitlement_term",
        "formal_entitlement",
        "term IN ('MONTH_1','MONTH_2','MONTH_3','MONTH_6','MONTH_12','PERMANENT')",
    )
    for column in ("entry_snapshot", "entry_version", "revision_id"):
        op.drop_column("favorite_source", column)
    for table in (
        "analytics_event",
        "ocr_quota_reservation",
        "ocr_monthly_usage",
        "ocr_settings",
        "scene_media_reference",
        "lexicon_entry_version",
        "lexicon_entry",
    ):
        op.drop_table(table)
    op.drop_column("batch_job", "result_payload")
    op.drop_column("batch_job", "input_payload")
    op.drop_column("content_series", "cover_asset_id")
    op.drop_column("media_asset", "security_request_id")
    # Keep widened sentence identifiers on rollback; narrowing would truncate existing stable IDs.
    for column in ("width", "height", "duration_ms"):
        op.drop_column("media_asset", column)
    op.execute("UPDATE schema_version SET version=14, updated_at=UTC_TIMESTAMP(6) WHERE id=1")
