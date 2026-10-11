"""Scope favorite identity to its explicitly saved content namespace."""

import sqlalchemy as sa
from alembic import context, op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Favorite content namespace upgrade requires online ownership checks")
    add_columns = True
    if not context.is_offline_mode():
        connection = op.get_bind()
        unresolved = connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM favorite_snapshot f "
                "LEFT JOIN favorite_snapshot_revision r "
                "ON r.public_id=f.current_snapshot_revision_id AND r.favorite_id=f.id "
                "WHERE r.id IS NULL OR r.content_type='' OR r.content_id=''"
            )
        )
        if unresolved:
            raise RuntimeError("Favorite content namespace cannot be proven from current revision")
        existing = connection.execute(
            sa.text(
                "SELECT COLUMN_NAME,DATA_TYPE,CHARACTER_MAXIMUM_LENGTH,COLLATION_NAME "
                "FROM information_schema.columns WHERE table_schema=DATABASE() "
                "AND table_name='favorite_snapshot' "
                "AND column_name IN ('content_type','content_id')"
            )
        ).all()
        if existing:
            expected = {
                ("content_type", "varchar", 32, "utf8mb4_bin"),
                ("content_id", "varchar", 64, "utf8mb4_bin"),
            }
            if {tuple(row) for row in existing} != expected:
                raise RuntimeError(
                    "Interrupted favorite namespace columns have an unexpected shape"
                )
            mismatch = connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM favorite_snapshot f "
                    "JOIN favorite_snapshot_revision r "
                    "ON r.public_id=f.current_snapshot_revision_id AND r.favorite_id=f.id "
                    "WHERE (f.content_type IS NOT NULL AND "
                    "CAST(f.content_type AS BINARY)<>CAST(r.content_type AS BINARY)) "
                    "OR (f.content_id IS NOT NULL AND "
                    "CAST(f.content_id AS BINARY)<>CAST(r.content_id AS BINARY))"
                )
            )
            if mismatch:
                raise RuntimeError("Interrupted favorite namespace contradicts current revision")
            add_columns = False
        identity = connection.scalar(
            sa.text(
                "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM information_schema.statistics WHERE table_schema=DATABASE() "
                "AND table_name='favorite_snapshot' AND index_name='uq_favorite_snapshot_1' "
                "AND NON_UNIQUE=0"
            )
        )
        if identity not in {
            "user_id,entry_type,entry_id,sense_id,source_id",
            "user_id,content_type,content_id,entry_type,entry_id,sense_id,source_id",
        }:
            raise RuntimeError("Favorite identity index has an unexpected shape")
    if add_columns:
        op.execute(
            "ALTER TABLE favorite_snapshot "
            "ADD COLUMN content_type VARCHAR(32) COLLATE utf8mb4_bin NULL AFTER user_id, "
            "ADD COLUMN content_id VARCHAR(64) COLLATE utf8mb4_bin NULL AFTER content_type"
        )
    op.execute(
        "UPDATE favorite_snapshot f JOIN favorite_snapshot_revision r "
        "ON r.public_id=f.current_snapshot_revision_id AND r.favorite_id=f.id "
        "SET f.content_type=r.content_type,f.content_id=r.content_id "
        "WHERE f.content_type IS NULL OR f.content_id IS NULL"
    )
    # Keep the old unique key until the new non-null identity is installed atomically.
    op.execute(
        "ALTER TABLE favorite_snapshot "
        "MODIFY COLUMN content_type VARCHAR(32) COLLATE utf8mb4_bin NOT NULL, "
        "MODIFY COLUMN content_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, "
        "DROP INDEX uq_favorite_snapshot_1, "
        "ADD UNIQUE INDEX uq_favorite_snapshot_1 "
        "(user_id,content_type,content_id,entry_type,entry_id,sense_id,source_id)"
    )
    op.execute("UPDATE schema_version SET version=19 WHERE id=1")


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "Favorite content namespace downgrade requires an online collision check"
        )
    collision = op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM favorite_snapshot "
            "GROUP BY user_id,entry_type,entry_id,sense_id,source_id "
            "HAVING COUNT(*)>1 LIMIT 1"
        )
    )
    if collision:
        raise RuntimeError(
            "Cannot downgrade: favorite content namespace collides under old identity"
        )
    # One atomic MySQL DDL also refuses a concurrent collision without dropping any columns.
    op.execute(
        "ALTER TABLE favorite_snapshot DROP INDEX uq_favorite_snapshot_1, "
        "ADD UNIQUE INDEX uq_favorite_snapshot_1 "
        "(user_id,entry_type,entry_id,sense_id,source_id), "
        "DROP COLUMN content_type, DROP COLUMN content_id"
    )
    op.execute("UPDATE schema_version SET version=18 WHERE id=1")
