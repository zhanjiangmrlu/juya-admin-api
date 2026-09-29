import copy
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import AppError

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def make_service() -> tuple[ContentService, InMemoryContentRepository]:
    repository = InMemoryContentRepository()
    for index in range(1, 9):
        scene_id = f"scene-{index}"
        series_id = "series-1" if index <= 7 else "series-2"
        status = "DRAFT" if index == 7 else "PUBLISHED"
        published_revision_id = None if status == "DRAFT" else f"published-{index}"
        repository.scenes[scene_id] = Scene(
            id=scene_id,
            series_id=series_id,
            title=f"Coffee scene {index}",
            summary=f"Summary {index}",
            status=status,
            draft_revision_id="draft-1" if index == 1 else None,
            published_revision_id=published_revision_id,
            updated_at=NOW,
        )
        if published_revision_id is not None:
            repository.revisions[published_revision_id] = SceneRevision(
                id=published_revision_id,
                scene_id=scene_id,
                source_revision_id=None,
                version=index,
                status="PUBLISHED",
                content={"title": f"Published {index}"},
                created_by="admin-1",
                created_at=NOW,
            )
    repository.revisions["draft-1"] = SceneRevision(
        id="draft-1",
        scene_id="scene-1",
        source_revision_id="published-1",
        version=3,
        status="DRAFT",
        content={"title": "Manual title", "dialogue": []},
        created_by="admin-1",
        created_at=NOW,
    )
    repository.revisions["ocr-1"] = SceneRevision(
        id="ocr-1",
        scene_id="scene-1",
        source_revision_id="draft-1",
        version=1,
        status="OCR_CANDIDATE",
        content={"title": "OCR title", "dialogue": ["candidate"]},
        created_by="admin-1",
        created_at=NOW,
    )
    return ContentService(repository), repository


def test_content_admin_service_exposes_planned_surface() -> None:
    service, _ = make_service()

    for method in (
        "list_scenes",
        "get_scene",
        "get_revision",
        "save_revision",
        "get_discovery_config",
        "save_discovery_config",
        "admin_preview",
    ):
        assert hasattr(service, method), method


@pytest.mark.asyncio
async def test_scene_catalog_filters_and_paginates_without_losing_total() -> None:
    service, _ = make_service()

    page = await service.list_scenes(
        page=2,
        page_size=2,
        query="coffee",
        series_id="series-1",
        status="PUBLISHED",
    )

    assert page.page == 2
    assert page.page_size == 2
    assert page.total == 6
    assert [scene.id for scene in page.items] == ["scene-3", "scene-4"]


@pytest.mark.asyncio
async def test_scene_and_revision_details_include_editor_versions() -> None:
    service, _ = make_service()

    scene = await service.get_scene("scene-1")
    revision = await service.get_revision("draft-1")

    assert scene.title == "Coffee scene 1"
    assert scene.draft_revision_id == "draft-1"
    assert revision.version == 3
    assert revision.content == {"title": "Manual title", "dialogue": []}


@pytest.mark.asyncio
async def test_revision_save_uses_optimistic_lock_and_keeps_ocr_candidate_separate() -> None:
    service, repository = make_service()

    with pytest.raises(AppError) as conflict:
        await service.save_revision(
            "draft-1",
            {"title": "Stale update"},
            expected_version=2,
            actor_id="admin-2",
        )

    assert conflict.value.code == "REVISION_VERSION_CONFLICT"
    assert conflict.value.details == {
        "current_revision_id": "draft-1",
        "current_version": 3,
    }
    assert repository.revisions["draft-1"].content["title"] == "Manual title"

    saved = await service.save_revision(
        "draft-1",
        {"title": "Reviewed title", "dialogue": []},
        expected_version=3,
        actor_id="admin-2",
    )

    assert saved.version == 4
    assert saved.content["title"] == "Reviewed title"
    assert repository.revisions["ocr-1"].content == {
        "title": "OCR title",
        "dialogue": ["candidate"],
    }


@pytest.mark.asyncio
async def test_revision_save_rejects_ocr_candidate_and_published_revision() -> None:
    service, _ = make_service()

    for revision_id in ("ocr-1", "published-1"):
        with pytest.raises(AppError) as exc:
            await service.save_revision(
                revision_id,
                {"title": "not allowed"},
                expected_version=1,
                actor_id="admin-2",
            )
        assert exc.value.code == "REVISION_NOT_EDITABLE"


@pytest.mark.asyncio
async def test_discovery_config_read_write_shares_one_version_and_enforces_rules() -> None:
    service, repository = make_service()

    initial = await service.get_discovery_config()
    assert initial.version == 0

    with pytest.raises(AppError) as module_error:
        await service.save_discovery_config(
            open_scene_ids=("scene-1", "scene-2", "scene-3"),
            preview_by_series={"series-1": ("scene-4", "scene-5", "scene-6")},
            learning_modules={"scene_learning": True, "grammar": True},
            expected_version=0,
            actor_id="admin-1",
            now=NOW,
        )
    assert module_error.value.code == "LEARNING_MODULE_NOT_ALLOWED"

    with pytest.raises(AppError) as preview_error:
        await service.save_discovery_config(
            open_scene_ids=("scene-1", "scene-2", "scene-3"),
            preview_by_series={"series-1": ("scene-1", "scene-4", "scene-5")},
            learning_modules={"scene_learning": True, "grammar": False},
            expected_version=0,
            actor_id="admin-1",
            now=NOW,
        )
    assert preview_error.value.code == "PREVIEW_SCENE_IS_OPEN"

    saved = await service.save_discovery_config(
        open_scene_ids=("scene-1", "scene-2", "scene-3"),
        preview_by_series={"series-1": ("scene-4", "scene-5", "scene-6")},
        learning_modules={"scene_learning": True, "grammar": False},
        expected_version=0,
        actor_id="admin-1",
        now=NOW,
    )
    assert saved.version == 1
    assert saved.open_scene_ids == ("scene-1", "scene-2", "scene-3")
    assert saved.preview_by_series == {"series-1": ("scene-4", "scene-5", "scene-6")}
    assert saved.learning_modules == {"scene_learning": True, "grammar": False}

    with pytest.raises(AppError) as conflict:
        await service.save_discovery_config(
            open_scene_ids=("scene-2", "scene-3", "scene-4"),
            preview_by_series={"series-1": ("scene-5", "scene-6", "scene-7")},
            learning_modules={"scene_learning": True},
            expected_version=0,
            actor_id="admin-2",
            now=NOW,
        )
    assert conflict.value.code == "DISCOVERY_CONFIG_VERSION_CONFLICT"
    assert conflict.value.details == {"current_version": 1}
    assert repository.discovery_config.version == 1


@pytest.mark.asyncio
async def test_legacy_discovery_commands_share_version_and_block_referenced_offline() -> None:
    service, _ = make_service()

    await service.replace_open_scenes(("scene-1", "scene-2", "scene-3"), "admin-1", NOW)
    await service.replace_preview_scenes(
        "series-1",
        ("scene-4", "scene-5", "scene-6"),
        "admin-1",
        NOW,
    )

    config = await service.get_discovery_config()
    assert config.version == 2
    assert config.open_scene_ids == ("scene-1", "scene-2", "scene-3")
    assert config.preview_by_series == {"series-1": ("scene-4", "scene-5", "scene-6")}

    with pytest.raises(AppError) as exc:
        await service.offline_scene("scene-4", "admin-1", NOW)
    assert exc.value.code == "PREVIEW_SCENE_CANNOT_OFFLINE"


@pytest.mark.asyncio
async def test_admin_preview_reads_unpublished_draft_without_side_effects() -> None:
    service, repository = make_service()
    before = copy.deepcopy(
        (
            repository.scenes,
            repository.revisions,
            repository.open_config,
            repository.preview_configs,
        )
    )

    preview = await service.admin_preview("draft-1")

    assert preview.scene_id == "scene-1"
    assert preview.revision_id == "draft-1"
    assert preview.revision_status == "DRAFT"
    assert preview.content["title"] == "Manual title"
    assert (
        repository.scenes,
        repository.revisions,
        repository.open_config,
        repository.preview_configs,
    ) == before


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


def test_content_editing_migration_adds_versions_and_catalog_index(
    mysql_connection: Connection,
) -> None:
    inspector = inspect(mysql_connection)
    revision_columns = {column["name"] for column in inspector.get_columns("scene_revision")}
    index_names = {index["name"] for index in inspector.get_indexes("scene")}

    assert "edit_version" in revision_columns
    assert "ix_scene_admin_catalog" in index_names
    assert (
        mysql_connection.scalar(text("SELECT version FROM discovery_config_state WHERE id = 1"))
        == 0
    )
    assert mysql_connection.scalar(text("SELECT version FROM schema_version WHERE id = 1")) >= 12
