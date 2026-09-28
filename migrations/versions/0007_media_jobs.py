"""Add media assets, provider jobs, batches, audio versions, and publish checks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.create_table(
        "media_asset",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("asset_type", sa.String(16), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", BIGINT, nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("security_status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.CHAR(26), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.UniqueConstraint("public_id", name="uq_media_asset_public_id"),
        sa.UniqueConstraint("object_key", name="uq_media_asset_object_key"),
        sa.UniqueConstraint("asset_type", "sha256", name="uq_media_asset_type_hash"),
        sa.CheckConstraint("asset_type IN ('images','audio')", name="ck_media_asset_type"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "batch_job",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("business_key", sa.String(191), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("total_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column(
            "success_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column(
            "failure_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("created_by", sa.CHAR(26), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.UniqueConstraint("public_id", name="uq_batch_job_public_id"),
        sa.UniqueConstraint("business_key", name="uq_batch_job_business_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "batch_job_item",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("batch_job_id", BIGINT, nullable=False),
        sa.Column("item_key", sa.String(191), nullable=False),
        sa.Column("target_id", sa.String(191), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "attempt_count", mysql.SMALLINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("result_version", mysql.INTEGER(unsigned=True)),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["batch_job_id"], ["batch_job.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_job_id", "item_key", name="uq_batch_job_item_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "ocr_candidate",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("asset_id", BIGINT, nullable=False),
        sa.Column("business_key", sa.String(191), nullable=False),
        sa.Column("provider_request_id", sa.String(191)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("template_type", sa.String(32), nullable=False),
        sa.Column("structured_candidate", mysql.JSON),
        sa.Column("confidence", sa.Numeric(6, 5)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.id"]),
        sa.UniqueConstraint("public_id", name="uq_ocr_candidate_public_id"),
        sa.UniqueConstraint("business_key", name="uq_ocr_candidate_business_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "audio_target",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("stable_key", sa.String(191), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("active_version_id", BIGINT),
        sa.UniqueConstraint("public_id", name="uq_audio_target_public_id"),
        sa.UniqueConstraint("stable_key", name="uq_audio_target_stable_key"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "audio_version",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("target_id", BIGINT, nullable=False),
        sa.Column("asset_id", BIGINT, nullable=False),
        sa.Column("version_no", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(191)),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["audio_target.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_asset.id"]),
        sa.UniqueConstraint("public_id", name="uq_audio_version_public_id"),
        sa.UniqueConstraint("target_id", "version_no", name="uq_audio_version_target_no"),
        sa.CheckConstraint("source IN ('MANUAL','TTS')", name="ck_audio_version_source"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_foreign_key(
        "fk_audio_target_active_version",
        "audio_target",
        "audio_version",
        ["active_version_id"],
        ["id"],
    )
    op.execute("UPDATE schema_version SET version = 7, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.drop_constraint("fk_audio_target_active_version", "audio_target", type_="foreignkey")
    op.drop_table("audio_version")
    op.drop_table("audio_target")
    op.drop_table("ocr_candidate")
    op.drop_table("batch_job_item")
    op.drop_table("batch_job")
    op.drop_table("media_asset")
    op.execute("UPDATE schema_version SET version = 6, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
