from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.pronunciation.domain import Config, Target
from juya_admin_api.modules.pronunciation.service import PronunciationService
from juya_admin_api.shared.errors import AppError


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
