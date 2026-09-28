from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.content.domain import PublishCheck, Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)


def make_service() -> tuple[ContentService, InMemoryContentRepository]:
    repository = InMemoryContentRepository()
    for index in range(1, 8):
        repository.scenes[f"scene-{index}"] = Scene(
            id=f"scene-{index}",
            series_id="series-1",
            status="PUBLISHED",
            published_revision_id=f"rev-{index}",
        )
        repository.revisions[f"rev-{index}"] = SceneRevision(
            id=f"rev-{index}",
            scene_id=f"scene-{index}",
            source_revision_id=None,
            status="PUBLISHED",
            stable_sentence_ids=(f"sentence-{index}",),
            stable_entry_ids=(f"entry-{index}",),
        )
    return ContentService(repository), repository


@pytest.mark.asyncio
async def test_open_config_requires_exactly_three_distinct_published_scenes() -> None:
    service, repository = make_service()

    for invalid in (
        ("scene-1", "scene-2"),
        ("scene-1", "scene-1", "scene-2"),
    ):
        with pytest.raises(AppError) as exc:
            await service.replace_open_scenes(invalid, "admin-1", NOW)  # type: ignore[arg-type]
        assert exc.value.code == "OPEN_SCENES_INVALID"

    repository.scenes["scene-3"].status = "OFFLINE"
    with pytest.raises(AppError) as exc:
        await service.replace_open_scenes(("scene-1", "scene-2", "scene-3"), "admin-1", NOW)
    assert exc.value.code == "OPEN_SCENE_NOT_PUBLISHED"

    repository.scenes["scene-3"].status = "PUBLISHED"
    config = await service.replace_open_scenes(("scene-1", "scene-2", "scene-3"), "admin-1", NOW)
    assert config.scene_ids == ("scene-1", "scene-2", "scene-3")
    assert config.version == 1


@pytest.mark.asyncio
async def test_preview_requires_three_to_six_published_non_open_scenes() -> None:
    service, _ = make_service()
    await service.replace_open_scenes(("scene-1", "scene-2", "scene-3"), "admin-1", NOW)

    with pytest.raises(AppError) as exc:
        await service.replace_preview_scenes(
            "series-1", ("scene-1", "scene-4", "scene-5"), "admin-1", NOW
        )
    assert exc.value.code == "PREVIEW_SCENE_IS_OPEN"

    config = await service.replace_preview_scenes(
        "series-1",
        ("scene-4", "scene-5", "scene-6"),
        "admin-1",
        NOW,
    )
    assert config.scene_ids == ("scene-4", "scene-5", "scene-6")


@pytest.mark.asyncio
async def test_new_revision_reuses_stable_sentence_and_entry_ids() -> None:
    service, _ = make_service()

    revision = await service.create_revision("scene-1", "rev-1", "admin-1", NOW)

    assert revision.status == "DRAFT"
    assert revision.stable_sentence_ids == ("sentence-1",)
    assert revision.stable_entry_ids == ("entry-1",)


@pytest.mark.asyncio
async def test_published_revision_is_immutable_and_open_scene_cannot_go_offline() -> None:
    service, _ = make_service()
    await service.replace_open_scenes(("scene-1", "scene-2", "scene-3"), "admin-1", NOW)

    with pytest.raises(AppError) as exc:
        await service.update_revision_content("rev-1", {"title": "changed"})
    assert exc.value.code == "PUBLISHED_REVISION_IMMUTABLE"

    with pytest.raises(AppError) as exc:
        await service.offline_scene("scene-1", "admin-1", NOW)
    assert exc.value.code == "OPEN_SCENE_CANNOT_OFFLINE"


@pytest.mark.asyncio
async def test_publish_requires_no_errors_and_all_warnings_acknowledged() -> None:
    service, repository = make_service()
    revision = await service.create_revision("scene-1", "rev-1", "admin-1", NOW)
    repository.publish_checks[revision.id] = [
        PublishCheck("MISSING_AUDIO", "WARNING", False),
    ]

    with pytest.raises(AppError) as exc:
        await service.validate_publish(revision.id, frozenset())
    assert exc.value.code == "PUBLISH_WARNING_NOT_ACKNOWLEDGED"

    summary = await service.validate_publish(revision.id, frozenset({"MISSING_AUDIO"}))
    assert summary.ready is True
