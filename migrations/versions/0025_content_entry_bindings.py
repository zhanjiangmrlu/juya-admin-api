"""Version-owned central references, explicit import scope and optional AI dismissal."""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE lexicon_candidate ADD dismissed_by VARCHAR(64) NULL, ADD dismissed_at "
        "DATETIME(6) NULL, ADD dismissal_reason TEXT NULL"
    )
    op.execute(
        "ALTER TABLE content_entry_revision ADD lexicon_entry_id VARCHAR(64) NULL, ADD "
        "lexicon_entry_version INTEGER NULL"
    )
    op.execute(
        "ALTER TABLE content_entry ADD import_namespace VARCHAR(64) NULL, ADD import_entry_id "
        "VARCHAR(64) NULL, ADD identity_provenance VARCHAR(32) NOT NULL DEFAULT 'legacy_unmapped'"
    )
    op.execute("""UPDATE content_entry e JOIN (
        SELECT entry_id,MIN(namespace) ns,MIN(import_entry_id) external_key,
        MIN(content_type) content_type,MIN(content_id) content_id,MIN(entry_type) entry_type
        FROM import_entry_mapping GROUP BY entry_id HAVING COUNT(*)=1
    ) m ON m.entry_id=e.public_id AND m.content_type=e.content_type
        AND m.content_id=e.content_id AND m.entry_type=e.entry_type
    SET e.import_namespace=m.ns,e.import_entry_id=m.external_key,
        e.identity_provenance='legacy_import_mapping'""")
    op.execute("UPDATE schema_version SET version=25 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Retain version bindings and audited human decisions")
