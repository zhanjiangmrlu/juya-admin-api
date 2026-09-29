"""Add optimistic content editing and unified discovery configuration versions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC_DATETIME = mysql.DATETIME(fsp=6)


def upgrade() -> None:
    op.add_column(
        "scene_revision",
        sa.Column(
            "edit_version",
            mysql.BIGINT(unsigned=True),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_index(
        "ix_scene_admin_catalog",
        "scene",
        ["status", "series_id", "updated_at"],
    )
    op.create_table(
        "discovery_config_state",
        sa.Column("id", mysql.TINYINT(unsigned=True), primary_key=True),
        sa.Column("version", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("updated_by", sa.CHAR(26)),
        sa.Column(
            "updated_at",
            UTC_DATETIME,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.CheckConstraint("id = 1", name="ck_discovery_config_singleton"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.execute(
        "INSERT INTO discovery_config_state (id, version, updated_at) "
        "SELECT 1, COALESCE(MAX(version), 0), UTC_TIMESTAMP(6) FROM open_scene_config"
    )
    op.execute("UPDATE schema_version SET version = 12, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")


def downgrade() -> None:
    op.execute("UPDATE schema_version SET version = 11, updated_at = UTC_TIMESTAMP(6) WHERE id = 1")
    op.drop_table("discovery_config_state")
    op.drop_index("ix_scene_admin_catalog", table_name="scene")
    op.drop_column("scene_revision", "edit_version")
