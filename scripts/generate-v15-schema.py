"""Freeze reviewed V1.5 metadata into an immutable migration and SQL contract."""

from pathlib import Path

from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateTable

from juya_admin_api.infrastructure.db.v15_schema import metadata
from juya_admin_api.modules.cloud_usage.models import metadata as cloud_metadata
from juya_admin_api.modules.pronunciation.models import metadata as pronunciation_metadata

root = Path(__file__).resolve().parents[1]
if (root / "migrations/versions/0017_v15_extension.py").exists():
    raise SystemExit("Migration 0017 is frozen; write a new incremental migration instead")
tables = [t for t in metadata.sorted_tables if not t.info.get("existing")]
tables.extend(pronunciation_metadata.sorted_tables)
tables.extend(cloud_metadata.sorted_tables)
ddl = [str(CreateTable(t).compile(dialect=dialect())).strip() for t in tables]
source = '''"""V1.5 additive schema. Frozen DDL; historical migrations remain unchanged."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

'''
source += "TABLE_DDL = [\n" + ",\n".join('    """' + sql + '"""' for sql in ddl) + "\n]\n\n"
source += """def upgrade() -> None:
    for statement in TABLE_DDL:
        op.execute(statement)
    op.alter_column("limited_campaign_version", "capacity",
                    existing_type=mysql.INTEGER(unsigned=True), nullable=True)
    op.execute("ALTER TABLE limited_campaign_version DROP CHECK ck_campaign_capacity")
    op.create_check_constraint("ck_campaign_capacity", "limited_campaign_version",
                               "capacity IS NULL OR "
                               "(capacity > 0 AND capacity >= granted_user_count)")
    op.add_column("learning_module_config", sa.Column("config_version", sa.Integer(),
                                                    nullable=False, server_default="1"))
    op.alter_column("scene_entry", "normalized_english", existing_type=sa.String(500),
                    type_=sa.Text(), existing_nullable=False)
    op.create_index("ix_import_stage_due", "content_import_stage",
                    ["status", "next_retry_at", "lease_until"])
    op.create_index("ix_import_stage_item", "content_import_stage", ["item_id", "stage", "status"])
    op.execute("UPDATE schema_version SET version=17 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Immutable V1.5 snapshots: rollback application flags, retain tables")
"""
(root / "migrations/versions/0017_v15_extension.py").write_text(source, encoding="utf-8")
(root / "docs/implementation/v15-schema.sql").write_text(
    ";\n\n".join(ddl)
    + ";\n"
    + """
ALTER TABLE limited_campaign_version MODIFY capacity INT UNSIGNED NULL;
ALTER TABLE limited_campaign_version DROP CHECK ck_campaign_capacity;
ALTER TABLE limited_campaign_version ADD CONSTRAINT ck_campaign_capacity
    CHECK (capacity IS NULL OR (capacity > 0 AND capacity >= granted_user_count));
ALTER TABLE learning_module_config ADD config_version INT NOT NULL DEFAULT 1;
ALTER TABLE scene_entry MODIFY normalized_english TEXT NOT NULL;
CREATE INDEX ix_import_stage_due ON content_import_stage (status,next_retry_at,lease_until);
CREATE INDEX ix_import_stage_item ON content_import_stage (item_id,stage,status);
UPDATE schema_version SET version=17 WHERE id=1;
""",
    encoding="utf-8",
)
print(f"Frozen {len(tables)} additive tables in migration 0017")
