from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.pronunciation.service import PronunciationService
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_saved_old_version_precedes_current_upload_and_legacy_never_synthesizes():
    for target in (
        Target("pron", "hello", uploaded_target_id="upload", uploaded_resource_version="v2"),
        None,
    ):
        repository = SimpleNamespace(
            target=AsyncMock(return_value=target), cached=AsyncMock(), enqueue=AsyncMock()
        )
        resources = AsyncMock(return_value={"resource_version": "v1", "url": "fixed"})
        context = {
            "purpose": "FAVORITE_REVIEW",
            "user_id": "user",
            "favorite_id": "favorite",
            "resource_version": "v1",
        }
        result = await PronunciationService(
            repository, Config(), AsyncMock(return_value=True), resources
        ).resolve("pron", context)
        assert result["resource_version"] == "v1"
        assert resources.call_args.args[1] == "v1"
        repository.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_favorite_uploaded_version_is_not_filled_with_current_version():
    repository = SimpleNamespace(
        target=AsyncMock(
            return_value=Target(
                "pron", "hello", uploaded_target_id="upload", uploaded_resource_version="v2"
            )
        )
    )
    resources = AsyncMock()
    with pytest.raises(AppError):
        await PronunciationService(
            repository, Config(), AsyncMock(return_value=True), resources
        ).resolve(
            "pron", {"purpose": "FAVORITE_REVIEW", "user_id": "user", "favorite_id": "favorite"}
        )
    resources.assert_not_awaited()


@pytest.mark.asyncio
async def test_uploaded_audio_priority_works_with_disabled_synthesis():
    repository = SimpleNamespace(
        target=AsyncMock(
            return_value=Target(
                "target", "hello", uploaded_target_id="uploaded", uploaded_resource_version="v1"
            )
        ),
        cached=AsyncMock(),
        enqueue=AsyncMock(),
    )
    resources = AsyncMock(return_value={"url": "signed-resource"})
    service = PronunciationService(repository, Config(), AsyncMock(return_value=True), resources)
    result = await service.resolve("target", {"purpose": "CONTENT", "user_id": "user"})
    assert result["source"] == "UPLOADED"
    repository.cached.assert_not_awaited()
    repository.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_favorite_authorization_rechecked_during_poll():
    repository = SimpleNamespace(
        task=AsyncMock(return_value={"public_id": "task", "target_id": "target", "status": "READY"})
    )
    service = PronunciationService(repository, Config(), AsyncMock(return_value=False), AsyncMock())
    with pytest.raises(AppError) as caught:
        await service.poll("task", {"purpose": "FAVORITE_REVIEW", "favorite_id": "favorite"})
    assert caught.value.status_code == 403
    service.resources.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_audio_resolves_with_disabled_synthesis():
    repository = SimpleNamespace(
        target=AsyncMock(return_value=Target("target", "hello")),
        cached=AsyncMock(return_value={"resource_version": "fixed"}),
        enqueue=AsyncMock(),
    )
    service = PronunciationService(
        repository,
        Config(),
        AsyncMock(return_value=True),
        AsyncMock(return_value={"url": "signed"}),
    )
    assert (await service.resolve("target", {"purpose": "FAVORITE_REVIEW"}))["source"] == "CACHE"
    repository.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_cached_task_poll_authorizes_callers_own_matching_target():
    config = Config()
    callers_target = Target("second-target", "hello")
    repository = SimpleNamespace(
        task=AsyncMock(
            return_value={
                "public_id": "task",
                "target_id": "first-target",
                "status": "READY",
                "cache_key": cache_key(callers_target, config),
                "config": asdict(config),
            }
        ),
        target=AsyncMock(return_value=callers_target),
    )
    authorize = AsyncMock(return_value=True)
    service = PronunciationService(
        repository, config, authorize, AsyncMock(return_value={"url": "signed"})
    )
    context = {"purpose": "FAVORITE_REVIEW", "target_id": "second-target"}
    assert (await service.poll("task", context))["status"] == "READY"
    authorize.assert_awaited_once_with("second-target", context)
