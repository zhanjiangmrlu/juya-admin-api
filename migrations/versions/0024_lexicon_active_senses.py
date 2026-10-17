"""Explicit active senses point to existing immutable dictionary definitions."""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE lexicon_active_sense (
        entry_id CHAR(26) NOT NULL, sense_key VARCHAR(64) NOT NULL, version INTEGER NOT NULL,
        updated_by VARCHAR(64) NOT NULL, updated_at DATETIME(6) NOT NULL,
        PRIMARY KEY(entry_id,sense_key), UNIQUE KEY uq_active_definition(entry_id,version),
        FOREIGN KEY(entry_id,version) REFERENCES lexicon_entry_version(entry_id,version)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    op.execute("""INSERT INTO lexicon_active_sense(entry_id,sense_key,version,updated_by,updated_at)
        SELECT e.public_id,CONCAT('legacy-',e.public_id),v.version,v.created_by,v.created_at
        FROM lexicon_entry e JOIN lexicon_entry_version v ON v.entry_id=e.public_id
        AND v.version=e.current_version""")
    op.execute("ALTER TABLE content_entry ADD lexicon_entry_version INTEGER NULL")
    op.execute("UPDATE schema_version SET version=24 WHERE id=1")


def downgrade() -> None:
    raise RuntimeError("Retain active sense decisions and pinned content references")
