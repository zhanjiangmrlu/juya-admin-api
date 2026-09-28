"""Create versioned content, open scenes, previews, and publish checks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | None = "0002"
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
        "learning_module_config",
        sa.Column("id", mysql.SMALLINT(unsigned=True), primary_key=True),
        sa.Column("module_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("sort_order", mysql.SMALLINT(unsigned=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("entry_path", sa.String(200)),
        sa.Column("display_rules", mysql.JSON),
        sa.UniqueConstraint("module_type", name="uq_learning_module_type"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "content_series",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False, server_default="0"),
        sa.Column("cover_object_key", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        _created_at(),
        sa.CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','OFFLINE')", name="ck_content_series_status"
        ),
        sa.UniqueConstraint("public_id", name="uq_content_series_public_id"),
        sa.UniqueConstraint("slug", name="uq_content_series_slug"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "content_template",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("template_type", sa.String(50), nullable=False),
        sa.Column("version", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("required_modules", mysql.JSON, nullable=False),
        sa.Column("validation_rules", mysql.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        _created_at(),
        sa.UniqueConstraint("template_type", "version", name="uq_content_template_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "scene",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("series_id", BIGINT, nullable=False),
        sa.Column("template_id", BIGINT, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(500)),
        sa.Column("cover_object_key", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("draft_revision_id", BIGINT),
        sa.Column("published_revision_id", BIGINT),
        _created_at(),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.ForeignKeyConstraint(["series_id"], ["content_series.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["content_template.id"]),
        sa.CheckConstraint("status IN ('DRAFT','PUBLISHED','OFFLINE')", name="ck_scene_status"),
        sa.UniqueConstraint("public_id", name="uq_scene_public_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "scene_revision",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("scene_id", BIGINT, nullable=False),
        sa.Column("source_revision_id", BIGINT),
        sa.Column("version_no", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("content_snapshot", mysql.JSON, nullable=False),
        sa.Column("created_by", sa.CHAR(26), nullable=False),
        _created_at(),
        sa.Column("published_at", UTC_DATETIME),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_revision_id"], ["scene_revision.id"]),
        sa.CheckConstraint(
            "status IN "
            "('DRAFT','OCR_CANDIDATE','REVIEWED','PUBLISH_READY','PUBLISHED','SUPERSEDED')",
            name="ck_scene_revision_status",
        ),
        sa.UniqueConstraint("public_id", name="uq_scene_revision_public_id"),
        sa.UniqueConstraint("scene_id", "version_no", name="uq_scene_revision_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_foreign_key(
        "fk_scene_draft_revision",
        "scene",
        "scene_revision",
        ["draft_revision_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_scene_published_revision",
        "scene",
        "scene_revision",
        ["published_revision_id"],
        ["id"],
    )
    op.create_table(
        "scene_dialogue_sentence",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("revision_id", BIGINT, nullable=False),
        sa.Column("stable_id", sa.CHAR(26), nullable=False),
        sa.Column("speaker", sa.String(100)),
        sa.Column("english_text", sa.Text(), nullable=False),
        sa.Column("chinese_text", sa.Text()),
        sa.Column("voice_role", sa.String(100)),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["scene_revision.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("revision_id", "stable_id", name="uq_sentence_revision_stable"),
        sa.UniqueConstraint("revision_id", "sort_order", name="uq_sentence_revision_order"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "scene_entry",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("revision_id", BIGINT, nullable=False),
        sa.Column("stable_id", sa.CHAR(26), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("normalized_english", sa.String(500), nullable=False),
        sa.Column("phonetic", sa.String(200)),
        sa.Column("chinese_text", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text()),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["scene_revision.id"], ondelete="CASCADE"),
        sa.CheckConstraint("entry_type IN ('VOCABULARY','PHRASE')", name="ck_scene_entry_type"),
        sa.UniqueConstraint("revision_id", "stable_id", name="uq_entry_revision_stable"),
        sa.UniqueConstraint("revision_id", "sort_order", name="uq_entry_revision_order"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "scene_entry_source",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("entry_id", BIGINT, nullable=False),
        sa.Column("source_stable_id", sa.CHAR(26), nullable=False),
        sa.Column("sentence_stable_id", sa.CHAR(26)),
        sa.Column("source_sentence_snapshot", sa.Text(), nullable=False),
        sa.Column("sort_order", mysql.INTEGER(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["scene_entry.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("entry_id", "source_stable_id", name="uq_entry_source_stable"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "publish_check_result",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("revision_id", BIGINT, nullable=False),
        sa.Column("rule_code", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("details", mysql.JSON),
        sa.Column("acknowledged_by", sa.CHAR(26)),
        sa.Column("acknowledged_at", UTC_DATETIME),
        _created_at(),
        sa.ForeignKeyConstraint(["revision_id"], ["scene_revision.id"], ondelete="CASCADE"),
        sa.CheckConstraint("severity IN ('ERROR','WARNING')", name="ck_publish_check_severity"),
        sa.UniqueConstraint("revision_id", "rule_code", name="uq_publish_check_rule"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "open_scene_config",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("version", BIGINT, nullable=False),
        sa.Column("activated_at", UTC_DATETIME, nullable=False),
        sa.Column("actor_public_id", sa.CHAR(26), nullable=False),
        _created_at(),
        sa.UniqueConstraint("version", name="uq_open_scene_config_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "open_scene_item",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("config_id", BIGINT, nullable=False),
        sa.Column("scene_id", BIGINT, nullable=False),
        sa.Column("position", mysql.TINYINT(unsigned=True), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["open_scene_config.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"]),
        sa.CheckConstraint("position BETWEEN 1 AND 3", name="ck_open_scene_position"),
        sa.UniqueConstraint("config_id", "scene_id", name="uq_open_scene_item_scene"),
        sa.UniqueConstraint("config_id", "position", name="uq_open_scene_item_position"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "preview_config",
        sa.Column("id", BIGINT, primary_key=True, autoincrement=True),
        sa.Column("series_id", BIGINT, nullable=False),
        sa.Column("scene_id", BIGINT, nullable=False),
        sa.Column("introduction", sa.String(500)),
        sa.Column("position", mysql.TINYINT(unsigned=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_by", sa.CHAR(26), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["series_id"], ["content_series.id"]),
        sa.ForeignKeyConstraint(["scene_id"], ["scene.id"]),
        sa.UniqueConstraint("series_id", "scene_id", name="uq_preview_config_scene"),
        sa.UniqueConstraint("series_id", "position", name="uq_preview_config_position"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute(
        "INSERT INTO learning_module_config "
        "(id, module_type, display_name, sort_order, enabled, entry_path) VALUES "
        "(1, 'scene_learning', '场景学习', 1, 1, '/learning/scenes'), "
        "(2, 'short_reading', '短文阅读', 2, 0, NULL), "
        "(3, 'long_sentence', '长难句', 3, 0, NULL), "
        "(4, 'grammar', '语法', 4, 0, NULL)"
    )
    op.execute("UPDATE schema_version SET version = 3, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.execute("UPDATE schema_version SET version = 2, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_table("preview_config")
    op.drop_table("open_scene_item")
    op.drop_table("open_scene_config")
    op.drop_table("publish_check_result")
    op.drop_table("scene_entry_source")
    op.drop_table("scene_entry")
    op.drop_table("scene_dialogue_sentence")
    op.drop_constraint("fk_scene_published_revision", "scene", type_="foreignkey")
    op.drop_constraint("fk_scene_draft_revision", "scene", type_="foreignkey")
    op.drop_table("scene_revision")
    op.drop_table("scene")
    op.drop_table("content_template")
    op.drop_table("content_series")
    op.drop_table("learning_module_config")
