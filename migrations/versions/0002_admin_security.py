"""Create administrator security, configuration, audit, and idempotency tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | None = "0001"
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
        "admin_user",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("totp_secret_ciphertext", sa.LargeBinary(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "failed_login_count",
            mysql.SMALLINT(unsigned=True),
            nullable=False,
            server_default="0",
        ),
        sa.Column("locked_until", UTC_DATETIME),
        sa.Column("last_totp_step", BIGINT),
        _created_at(),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_admin_user_status"),
        sa.UniqueConstraint("public_id", name="uq_admin_user_public_id"),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "admin_auth_challenge",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("admin_user_id", BIGINT, nullable=False),
        sa.Column("client_ip_hash", sa.CHAR(64), nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.Column("consumed_at", UTC_DATETIME),
        _created_at(),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_user.id"], ondelete="CASCADE"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_admin_auth_challenge_expiry",
        "admin_auth_challenge",
        ["expires_at"],
    )
    op.create_table(
        "admin_session",
        sa.Column("id", sa.CHAR(26), primary_key=True),
        sa.Column("admin_user_id", BIGINT, nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("csrf_hash", sa.CHAR(64), nullable=False),
        sa.Column("client_ip_hash", sa.CHAR(64)),
        sa.Column("device_summary", sa.String(200), nullable=False),
        sa.Column("expires_at", UTC_DATETIME, nullable=False),
        sa.Column("revoked_at", UTC_DATETIME),
        sa.Column("last_seen_at", UTC_DATETIME),
        _created_at(),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_admin_session_expiry", "admin_session", ["expires_at"])
    op.create_table(
        "audit_event",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("actor_public_id", sa.CHAR(26)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(100), nullable=False),
        sa.Column("object_public_id", sa.String(64), nullable=False),
        sa.Column("before_summary", mysql.JSON),
        sa.Column("after_summary", mysql.JSON),
        sa.Column("reason", sa.String(500)),
        sa.Column("request_id", sa.String(64), nullable=False),
        _created_at(),
        sa.UniqueConstraint("public_id", name="uq_audit_event_public_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(
        "ix_audit_event_object",
        "audit_event",
        ["object_type", "object_public_id", "created_at"],
    )
    op.create_table(
        "system_config",
        sa.Column("config_key", sa.String(100), primary_key=True),
        sa.Column("value", mysql.JSON, nullable=False),
        sa.Column("version", BIGINT, nullable=False, server_default="1"),
        sa.Column("updated_by", sa.CHAR(26)),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "idempotency_record",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="IN_PROGRESS"),
        sa.Column("response_status", mysql.SMALLINT(unsigned=True)),
        sa.Column("response_body", mysql.JSON),
        sa.Column("expires_at", UTC_DATETIME),
        _created_at(),
        sa.Column("completed_at", UTC_DATETIME),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS','COMPLETED','FAILED')",
            name="ck_idempotency_record_status",
        ),
        sa.UniqueConstraint(
            "scope",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_record_identity",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "admin_outbox",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.CHAR(26), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("aggregate_public_id", sa.String(64), nullable=False),
        sa.Column("payload", mysql.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column(
            "attempt_count",
            mysql.INTEGER(unsigned=True),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_attempt_at", UTC_DATETIME),
        _created_at(),
        sa.Column("published_at", UTC_DATETIME),
        sa.UniqueConstraint("event_id", name="uq_admin_outbox_event_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_admin_outbox_dispatch", "admin_outbox", ["status", "next_attempt_at"])
    op.execute(
        "INSERT INTO system_config (config_key, value, version) VALUES "
        "('feedback_sla_hours', JSON_OBJECT('value', 24), 1), "
        "('entitlement_expiry_warning_days', JSON_OBJECT('value', 7), 1), "
        "('shadowing_enabled', JSON_OBJECT('value', true), 1), "
        "('readonly_preview_enabled', JSON_OBJECT('value', true), 1), "
        "('unentitled_material_entry_enabled', JSON_OBJECT('value', true), 1), "
        "('asset_signed_url_ttl_seconds', JSON_OBJECT('value', 300), 1)"
    )
    op.execute("UPDATE schema_version SET version = 2, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.execute("UPDATE schema_version SET version = 1, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_index("ix_admin_outbox_dispatch", table_name="admin_outbox")
    op.drop_table("admin_outbox")
    op.drop_table("idempotency_record")
    op.drop_table("system_config")
    op.drop_index("ix_audit_event_object", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_admin_session_expiry", table_name="admin_session")
    op.drop_table("admin_session")
    op.drop_index("ix_admin_auth_challenge_expiry", table_name="admin_auth_challenge")
    op.drop_table("admin_auth_challenge")
    op.drop_table("admin_user")
