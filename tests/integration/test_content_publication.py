import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from juya_admin_api.modules.content.domain import Scene
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def mysql_connection() -> Connection:
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    connection = engine.connect()
    yield connection
    connection.close()
    engine.dispose()


def test_content_schema_keeps_scene_revision_versions_unique(
    mysql_connection: Connection,
) -> None:
    with mysql_connection.begin():
        mysql_connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "preview_config",
            "open_scene_item",
            "open_scene_config",
            "publish_check_result",
            "scene_entry_source",
            "scene_entry",
            "scene_dialogue_sentence",
            "scene_revision",
            "scene",
            "content_template",
            "content_series",
        ):
            mysql_connection.execute(text(f"DELETE FROM {table}"))
        mysql_connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        mysql_connection.execute(
            text(
                "INSERT INTO content_series (public_id, slug, title, status) "
                "VALUES ('01J00000000000000000000200', 'daily', 'Daily', 'PUBLISHED')"
            )
        )
        series_id = mysql_connection.scalar(text("SELECT id FROM content_series"))
        mysql_connection.execute(
            text(
                "INSERT INTO content_template "
                "(template_type, version, required_modules, validation_rules) "
                "VALUES ('dialogue', 1, JSON_ARRAY(), JSON_OBJECT())"
            )
        )
        template_id = mysql_connection.scalar(text("SELECT id FROM content_template"))
        mysql_connection.execute(
            text(
                "INSERT INTO scene (public_id, series_id, template_id, title, status) "
                "VALUES ('01J00000000000000000000201', :series_id, :template_id, "
                "'Scene', 'DRAFT')"
            ),
            {"series_id": series_id, "template_id": template_id},
        )
        scene_id = mysql_connection.scalar(text("SELECT id FROM scene"))
        mysql_connection.execute(
            text(
                "INSERT INTO scene_revision "
                "(public_id, scene_id, version_no, status, content_snapshot, created_by) "
                "VALUES ('01J00000000000000000000202', :scene_id, 1, 'DRAFT', "
                "JSON_OBJECT(), '01J00000000000000000000100')"
            ),
            {"scene_id": scene_id},
        )

    with pytest.raises(IntegrityError), mysql_connection.begin():
        mysql_connection.execute(
            text(
                "INSERT INTO scene_revision "
                "(public_id, scene_id, version_no, status, content_snapshot, created_by) "
                "VALUES ('01J00000000000000000000203', :scene_id, 1, 'DRAFT', "
                "JSON_OBJECT(), '01J00000000000000000000100')"
            ),
            {"scene_id": scene_id},
        )

    version = mysql_connection.scalar(text("SELECT version FROM schema_version WHERE id = 1"))
    assert version >= 3


@pytest.mark.asyncio
async def test_publication_switches_revision_atomically_and_is_idempotent() -> None:
    repository = InMemoryContentRepository()
    repository.scenes["scene-1"] = Scene("scene-1", "series-1")
    service = ContentService(repository)
    revision = await service.create_revision("scene-1", None, "admin-1", NOW)

    first = await service.publish_revision(revision.id, "admin-1", "publish-1", NOW)
    replay = await service.publish_revision(revision.id, "admin-1", "publish-1", NOW)

    assert first == replay
    assert repository.scenes["scene-1"].published_revision_id == revision.id
    assert repository.revisions[revision.id].status == "PUBLISHED"
