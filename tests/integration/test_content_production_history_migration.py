"""Real old favorite/resource/paid history survives every migration to the final head."""

from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_admin_contact_schema import legacy_schema  # noqa: F401
from tests.integration.test_v15_favorite_identity_migration import config, rows, seed_old


def test_0018_to_final_head_preserves_actual_favorite_revisions_resources_and_paid_attempt(
    legacy_schema,  # noqa: F811
):
    migration = config()
    command.upgrade(migration, "0018")
    engine = create_engine(legacy_schema)
    try:
        with engine.begin() as connection:
            seed_old(connection)
            tables = (
                "favorite_snapshot",
                "favorite_snapshot_revision",
                "favorite_resource_reference",
                "content_cloud_attempt",
            )
            before = {table: rows(connection, table) for table in tables}
            assert [len(before[table]) for table in tables] == [1, 2, 1, 1]
        command.upgrade(migration, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id=1")) == 25
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0025"
            assert {table: rows(connection, table) for table in tables} == before
            assert connection.execute(
                text("SELECT content_type,content_id FROM favorite_snapshot")
            ).one() == ("word_card", "article-A")
        # Upgrade replay is also read-safe at the actual final schema.
        command.upgrade(migration, "head")
        with engine.connect() as connection:
            assert {table: rows(connection, table) for table in tables} == before
    finally:
        engine.dispose()
