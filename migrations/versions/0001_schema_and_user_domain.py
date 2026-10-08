"""Create schema version and miniapp-owned user domain tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0001"
down_revision: str | None = None
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
    # 功能:创建版本标记及小程序用户、会话、联系方式、学习、收藏和注销领域表。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    op.create_table(
        "schema_version",
        sa.Column("id", mysql.SMALLINT(unsigned=True), primary_key=True, autoincrement=False),
        sa.Column("version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            server_onupdate=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")

    op.create_table(
        "user_account",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("juya_number", sa.String(20), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        _created_at(),
        sa.Column("last_active_at", UTC_DATETIME),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DELETION_PENDING','DELETING','DELETED','SUSPENDED')",
            name="ck_user_account_status",
        ),
        sa.UniqueConstraint("public_id", name="uq_user_account_public_id"),
        sa.UniqueConstraint("juya_number", name="uq_user_account_juya_number"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_user_account_status", "user_account", ["status"])

    op.create_table(
        "user_app_identity",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("openid_ciphertext", sa.LargeBinary(1024), nullable=False),
        sa.Column("openid_hmac", mysql.BINARY(32), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("app_id", "openid_hmac", name="uq_user_app_identity_openid"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_app_identity_user", "user_app_identity", ["user_id"])

    op.create_table(
        "user_profile",
        sa.Column("user_id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("nickname", sa.String(64)),
        sa.Column("avatar_object_key", sa.String(512)),
        sa.Column("source", sa.String(32), nullable=False, server_default="WECHAT"),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_profile_nickname", "user_profile", ["nickname"])

    op.create_table(
        "user_session",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("refresh_token_hash", mysql.BINARY(32), nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.Column("revoked_at", UTC_DATETIME),
        sa.Column("device_digest", mysql.BINARY(32)),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_user_session_refresh_hash"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_session_user", "user_session", ["user_id", "revoked_at"])

    op.create_table(
        "user_contact",
        sa.Column("user_id", BIGINT, primary_key=True, autoincrement=False),
        sa.Column("wechat_id_ciphertext", sa.LargeBinary(1024)),
        sa.Column("wechat_id_hmac", mysql.BINARY(32)),
        sa.Column("consent_version", sa.String(32)),
        sa.Column("consented_at", UTC_DATETIME),
        sa.Column("source", sa.String(32)),
        sa.Column(
            "self_edit_count", mysql.SMALLINT(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("withdrawn_at", UTC_DATETIME),
        sa.Column("change_pending", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("contact_status", sa.String(32), nullable=False, server_default="NOT_PROVIDED"),
        sa.Column("verified_at", UTC_DATETIME),
        sa.Column("verified_by", sa.CHAR(26)),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.CheckConstraint("self_edit_count <= 1", name="ck_user_contact_edit_count"),
        sa.CheckConstraint(
            "contact_status IN ('NOT_PROVIDED','PENDING','CONTACTED','VERIFIED','INVALID')",
            name="ck_user_contact_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_user_contact_wechat_hmac", "user_contact", ["wechat_id_hmac"])

    op.create_table(
        "contact_status_history",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("occurred_at", UTC_DATETIME, nullable=False),
        sa.Column("note", sa.String(500)),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_contact_status_history_user_time",
        "contact_status_history",
        ["user_id", "occurred_at"],
    )

    op.create_table(
        "contact_correction_request",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        _created_at(),
        sa.Column("processed_at", UTC_DATETIME),
        sa.Column(
            "active_user_id",
            BIGINT,
            sa.Computed(
                "CASE WHEN `status` IN ('PENDING','PROCESSING') THEN `user_id` ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','APPROVED','REJECTED','CANCELLED')",
            name="ck_contact_correction_status",
        ),
        sa.UniqueConstraint("public_id", name="uq_contact_correction_public_id"),
        sa.UniqueConstraint("active_user_id", name="uq_contact_correction_active_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "learning_progress",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("position", mysql.JSON, nullable=False),
        sa.Column("last_client_sequence", BIGINT, nullable=False, server_default="0"),
        sa.Column("started_at", UTC_DATETIME, nullable=False),
        sa.Column("completed_at", UTC_DATETIME),
        sa.Column("last_learned_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "scene_id", name="uq_learning_progress_user_scene"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_learning_progress_user_last",
        "learning_progress",
        ["user_id", "last_learned_at"],
    )

    op.create_table(
        "learning_completion_event",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("completed_at", UTC_DATETIME, nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_learning_completion_idempotency"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "daily_checkin",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("beijing_date", sa.Date(), nullable=False),
        sa.Column("completion_source", sa.String(32), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "beijing_date", name="uq_daily_checkin_user_date"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "favorite_entry",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("entry_type", sa.String(32), nullable=False),
        sa.Column("normalized_key", sa.String(255), nullable=False),
        sa.Column("entry_stable_id", sa.String(64), nullable=False),
        sa.Column("favorited_at", UTC_DATETIME, nullable=False),
        sa.Column("last_reviewed_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.CheckConstraint("entry_type IN ('VOCABULARY','PHRASE')", name="ck_favorite_entry_type"),
        sa.UniqueConstraint("public_id", name="uq_favorite_entry_public_id"),
        sa.UniqueConstraint(
            "user_id", "entry_type", "normalized_key", name="uq_favorite_entry_normalized"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "favorite_source",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("favorite_id", BIGINT, nullable=False),
        sa.Column("scene_id", sa.String(64), nullable=False),
        sa.Column("sentence_snapshot", sa.Text(), nullable=False),
        sa.Column("source_locator", sa.String(255), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["favorite_id"], ["favorite_entry.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "favorite_id", "scene_id", "source_locator", name="uq_favorite_source_location"
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "review_session",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("review_type", sa.String(32), nullable=False),
        sa.Column("started_at", UTC_DATETIME, nullable=False),
        sa.Column("completed_at", UTC_DATETIME),
        sa.Column("card_count", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_review_session_idempotency"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "inbox_message",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("related_type", sa.String(64)),
        sa.Column("related_id", sa.String(64)),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        _created_at(),
        sa.Column("read_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_id", name="uq_inbox_message_event"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_inbox_message_user_read", "inbox_message", ["user_id", "read_at"])

    op.create_table(
        "account_deletion_request",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("requested_at", UTC_DATETIME, nullable=False),
        sa.Column("effective_at", UTC_DATETIME, nullable=False),
        sa.Column("revoked_at", UTC_DATETIME),
        sa.Column("completed_at", UTC_DATETIME),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "active_user_id",
            BIGINT,
            sa.Computed(
                "CASE WHEN `status` IN ('PENDING','DELETING') THEN `user_id` ELSE NULL END",
                persisted=True,
            ),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"]),
        sa.CheckConstraint(
            "status IN ('PENDING','REVOKED','DELETING','DELETED','FAILED')",
            name="ck_account_deletion_status",
        ),
        sa.UniqueConstraint("public_id", name="uq_account_deletion_public_id"),
        sa.UniqueConstraint("active_user_id", name="uq_account_deletion_active_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "user_command_dedup",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("user_id", BIGINT, nullable=False),
        sa.Column("endpoint", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", mysql.BINARY(32), nullable=False),
        sa.Column("response_snapshot", mysql.JSON),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "endpoint", "idempotency_key", name="uq_user_command_dedup"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "miniapp_outbox",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("payload", mysql.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column(
            "attempt_count", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"
        ),
        sa.Column("next_attempt_at", UTC_DATETIME),
        _created_at(),
        sa.Column("processed_at", UTC_DATETIME),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','DELIVERED','FAILED','DEAD')",
            name="ck_miniapp_outbox_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_miniapp_outbox_dispatch", "miniapp_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    # 功能:删除小程序用户领域表及版本标记,撤销初始数据库结构。
    # 参数:无。
    # 返回:无, 通过 Alembic 操作变更数据库结构或迁移数据。
    for table_name in (
        "miniapp_outbox",
        "user_command_dedup",
        "account_deletion_request",
        "inbox_message",
        "review_session",
        "favorite_source",
        "favorite_entry",
        "daily_checkin",
        "learning_completion_event",
        "learning_progress",
        "contact_correction_request",
        "contact_status_history",
        "user_contact",
        "user_session",
        "user_profile",
        "user_app_identity",
        "user_account",
        "schema_version",
    ):
        op.drop_table(table_name)
