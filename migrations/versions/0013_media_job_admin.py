"""Persist admin media jobs, audio candidates, and draft trash state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = mysql.BIGINT(unsigned=True)
UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    # 功能:持久化媒体处理任务、音频候选和草稿回收站状态。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.add_column("batch_job", sa.Column("completed_at", UTC_DATETIME))
    op.add_column("batch_job", sa.Column("cancel_requested_at", UTC_DATETIME))
    op.create_index(
        "ix_batch_job_admin",
        "batch_job",
        ["status", "job_type", "created_at"],
    )

    op.create_table(
        "processing_job",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("business_key", sa.String(191), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(191), nullable=False),
        sa.Column("batch_job_id", BIGINT),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_request_id", sa.String(191)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_by", sa.CHAR(26), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.Column("cancel_requested_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["batch_job_id"], ["batch_job.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("public_id", name="uq_processing_job_public_id"),
        sa.UniqueConstraint("business_key", name="uq_processing_job_business_key"),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_processing_job_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_processing_job_admin",
        "processing_job",
        ["status", "job_type", "created_at"],
    )

    op.add_column("batch_job_item", sa.Column("public_id", sa.CHAR(26)))
    op.add_column("batch_job_item", sa.Column("processing_job_id", BIGINT))
    op.create_unique_constraint(
        "uq_batch_job_item_public_id",
        "batch_job_item",
        ["public_id"],
    )
    op.create_foreign_key(
        "fk_batch_job_item_processing_job",
        "batch_job_item",
        "processing_job",
        ["processing_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_batch_job_item_status",
        "batch_job_item",
        ["batch_job_id", "status", "id"],
    )

    op.add_column("ocr_candidate", sa.Column("processing_job_id", BIGINT))
    op.add_column("ocr_candidate", sa.Column("confirmed_revision_id", BIGINT))
    op.add_column("ocr_candidate", sa.Column("confirmed_by", sa.CHAR(26)))
    op.add_column("ocr_candidate", sa.Column("confirmed_at", UTC_DATETIME))
    op.create_unique_constraint(
        "uq_ocr_candidate_processing_job",
        "ocr_candidate",
        ["processing_job_id"],
    )
    op.create_foreign_key(
        "fk_ocr_candidate_processing_job",
        "ocr_candidate",
        "processing_job",
        ["processing_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_ocr_candidate_confirmed_revision",
        "ocr_candidate",
        "scene_revision",
        ["confirmed_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ocr_candidate_admin",
        "ocr_candidate",
        ["status", "created_at"],
    )

    op.add_column("audio_version", sa.Column("processing_job_id", BIGINT))
    op.add_column("audio_version", sa.Column("created_by", sa.CHAR(26)))
    op.create_unique_constraint(
        "uq_audio_version_processing_job",
        "audio_version",
        ["processing_job_id"],
    )
    op.create_unique_constraint(
        "uq_audio_version_provider_request",
        "audio_version",
        ["provider_request_id"],
    )
    op.create_foreign_key(
        "fk_audio_version_processing_job",
        "audio_version",
        "processing_job",
        ["processing_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_audio_version_admin",
        "audio_version",
        ["target_id", "status", "version_no"],
    )

    op.create_table(
        "draft_trash",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("scene_public_id", sa.CHAR(26), nullable=False),
        sa.Column("revision_public_id", sa.CHAR(26), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("trashed_by", sa.CHAR(26), nullable=False),
        sa.Column("trashed_at", UTC_DATETIME, nullable=False),
        sa.Column("retention_until", UTC_DATETIME, nullable=False),
        sa.Column("restored_at", UTC_DATETIME),
        sa.Column("cleaned_at", UTC_DATETIME),
        sa.UniqueConstraint("public_id", name="uq_draft_trash_public_id"),
        sa.UniqueConstraint("revision_public_id", name="uq_draft_trash_revision"),
        sa.CheckConstraint(
            "status IN ('TRASHED','RESTORED','CLEANED')",
            name="ck_draft_trash_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_draft_trash_admin",
        "draft_trash",
        ["status", "retention_until", "trashed_at"],
    )

    op.execute("UPDATE schema_version SET version = 13, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    # 功能:删除媒体处理任务与回收站表并撤销相关字段。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.execute("UPDATE schema_version SET version = 12, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_table("draft_trash")

    op.drop_index("ix_audio_version_admin", table_name="audio_version")
    op.drop_constraint("fk_audio_version_processing_job", "audio_version", type_="foreignkey")
    op.drop_constraint("uq_audio_version_provider_request", "audio_version", type_="unique")
    op.drop_constraint("uq_audio_version_processing_job", "audio_version", type_="unique")
    op.drop_column("audio_version", "created_by")
    op.drop_column("audio_version", "processing_job_id")

    op.drop_index("ix_ocr_candidate_admin", table_name="ocr_candidate")
    op.drop_constraint(
        "fk_ocr_candidate_confirmed_revision",
        "ocr_candidate",
        type_="foreignkey",
    )
    op.drop_constraint("fk_ocr_candidate_processing_job", "ocr_candidate", type_="foreignkey")
    op.drop_constraint("uq_ocr_candidate_processing_job", "ocr_candidate", type_="unique")
    op.drop_column("ocr_candidate", "confirmed_at")
    op.drop_column("ocr_candidate", "confirmed_by")
    op.drop_column("ocr_candidate", "confirmed_revision_id")
    op.drop_column("ocr_candidate", "processing_job_id")

    op.drop_index("ix_batch_job_item_status", table_name="batch_job_item")
    op.drop_constraint(
        "fk_batch_job_item_processing_job",
        "batch_job_item",
        type_="foreignkey",
    )
    op.drop_constraint("uq_batch_job_item_public_id", "batch_job_item", type_="unique")
    op.drop_column("batch_job_item", "processing_job_id")
    op.drop_column("batch_job_item", "public_id")

    op.drop_table("processing_job")
    op.drop_index("ix_batch_job_admin", table_name="batch_job")
    op.drop_column("batch_job", "cancel_requested_at")
    op.drop_column("batch_job", "completed_at")
