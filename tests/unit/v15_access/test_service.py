from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.content.v15_contracts import (
    ConfirmationCredential,
    ConfirmedEntry,
    FavoriteReference,
    FavoriteResourceEvent,
    PronunciationReference,
    ResourceReference,
    SceneDetail,
)
from juya_admin_api.modules.v15_access.ports import AccessFacts, FavoriteFacts
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 11, tzinfo=UTC)


class Session:
    @asynccontextmanager
    async def begin(self):
        yield self


class Sessions:
    @asynccontextmanager
    async def __call__(self):
        yield Session()


def detail():
    entry = ConfirmedEntry(
        entry_id="entry",
        entry_type="word",
        sense_id="sense",
        source_id="source",
        english="medium",
        meaning_zh="中杯",
        source_sentence="A medium coffee.",
        image_asset_id="image",
        pronunciation=PronunciationReference(target_id="word-audio", resource_version="word-v1"),
    )
    return SceneDetail(
        content_id="scene",
        content_type="dialogue",
        content_version=1,
        revision_id="revision",
        title_en="Safe title",
        title_zh="安全标题",
        words=[entry],
        whole_audio=ResourceReference(
            target_id="whole", resource_version="whole-v1", duration_ms=100
        ),
    )


def reference():
    return FavoriteReference(
        content_type="dialogue",
        content_id="scene",
        content_version=1,
        entry_id="entry",
        sense_id="sense",
        source_id="source",
    )


def saved():
    return FavoriteFacts(
        "favorite",
        "snapshot",
        "dialogue",
        "scene",
        "revision",
        1,
        "entry",
        "sense",
        "source",
        "word",
        "image",
        "word-audio",
        "word-v1",
        "Safe title",
        "安全标题",
    )


def service(facts=None):
    return V15AccessService(
        Sessions(),
        content_loader=AsyncMock(return_value=detail()),
        authorizer=AsyncMock(return_value=facts or AccessFacts(True, True, 1, "auth-v1")),
        resource_signer=AsyncMock(return_value={"url": "private-short-url"}),
        credential_secret=b"unit-private-signing-secret",
        favorite_authority=AsyncMock(return_value=saved()),
    )


@pytest.mark.asyncio
async def test_preview_is_safe_whitelist_even_when_complete_dto_is_loaded():
    svc = service(AccessFacts(False, True, 1, "auth"))
    preview = await svc.preview("user", "dialogue", "scene", NOW)
    assert preview["title_en"] == "Safe title"
    assert {"words", "chunks", "dialogue", "whole_audio", "revision_id"}.isdisjoint(preview)
    with pytest.raises(AppError):
        await svc.open("user", "dialogue", "scene", NOW)


@pytest.mark.asyncio
async def test_preview_cover_uses_only_server_current_cover_and_live_visible_permission():
    svc = service(AccessFacts(False, True, 1, "preview"))
    svc.content_loader.return_value = detail().model_copy(update={"cover_asset_id": "cover"})
    assert (await svc.preview_cover("user", "dialogue", "scene", NOW))["url"]
    request = svc.resource_signer.call_args.args[1]
    assert (request.target_id, request.asset_id, request.role) == ("cover", "cover", "COVER")
    assert request.preview_only and request.favorite_id is None
    assert svc.authorizer.call_args.kwargs["lock"] is True
    with pytest.raises(AppError):
        await svc.content_resource("user", "dialogue", "scene", "whole", "whole-v1", NOW)
    svc.resource_signer.reset_mock()
    svc.authorizer.return_value = AccessFacts(False, False, 2, "disabled")
    with pytest.raises(AppError):
        await svc.preview_cover("user", "dialogue", "scene", NOW)
    svc.resource_signer.assert_not_awaited()
    svc.authorizer.return_value = AccessFacts(False, True, 2, "visible")
    svc.content_loader.return_value = detail()
    with pytest.raises(AppError) as absent:
        await svc.preview_cover("user", "dialogue", "scene", NOW)
    assert absent.value.code == "CONTENT_COVER_MISSING"


@pytest.mark.asyncio
async def test_confirmation_pins_user_entry_hash_versions_and_shared_lock():
    svc = service(AccessFacts(True, True, 7, "auth-v8", NOW + timedelta(seconds=10)))
    result = await svc.confirmation("user", reference(), NOW)
    credential = ConfirmationCredential.model_validate(result["credential"])
    assert datetime.fromisoformat(credential.expires_at) == NOW + timedelta(seconds=10)
    assert credential.module_version == 7
    assert svc.authorizer.call_args.kwargs["lock"] is True
    entry = await svc.verify_confirmation_within(Session(), credential, "user", NOW)
    assert entry.meaning_zh == "中杯"
    with pytest.raises(AppError):
        await svc.verify_confirmation_within(Session(), credential, "another-user", NOW)
    svc.authorizer.return_value = AccessFacts(True, True, 8, "auth-v8")
    with pytest.raises(AppError):
        await svc.verify_confirmation_within(Session(), credential, "user", NOW)


@pytest.mark.asyncio
async def test_changed_details_or_source_cannot_be_saved_using_old_credential():
    svc = service()
    credential = ConfirmationCredential.model_validate(
        (await svc.confirmation("user", reference(), NOW))["credential"]
    )
    changed = detail()
    changed.words[0].meaning_zh = "中等"
    svc.content_loader.return_value = changed
    with pytest.raises(AppError):
        await svc.verify_confirmation_within(Session(), credential, "user", NOW)
    invalid = reference().model_copy(update={"source_id": "other-source"})
    with pytest.raises(AppError):
        await svc.confirmation("user", invalid, NOW)


@pytest.mark.asyncio
async def test_no_access_source_has_no_current_version_or_precise_location():
    svc = service(AccessFacts(False, False, 1, "expired"))
    result = await svc.source_resolution("user", "favorite", NOW)
    assert result.status == "NO_ACCESS"
    assert result.target_content_version is None and result.locator is None
    svc.content_loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_review_survives_source_module_offline_and_expiry():
    svc = service(AccessFacts(False, False, 2, "offline"))
    await svc.review_resource("user", "favorite", "word-audio", "word-v1", NOW)
    svc.authorizer.assert_not_awaited()
    request = svc.resource_signer.call_args.args[1]
    assert request.role == "WORD"
    assert svc.resource_signer.call_args.args[2] <= NOW + timedelta(seconds=60)
    for target, version in [
        ("whole", "whole-v1"),
        ("word-audio", "new-version"),
        ("original", None),
    ]:
        with pytest.raises(AppError):
            await svc.review_resource("user", "favorite", target, version, NOW)
    svc.favorite_authority.side_effect = AppError("FAVORITE_NOT_FOUND", "不存在", 404)
    with pytest.raises(AppError):
        await svc.review_resource("another-user", "favorite", "word-audio", "word-v1", NOW)


def test_chunk_review_has_no_image_and_full_content_cannot_use_stale_resources():
    chunk = replace(saved(), entry_type="chunk")
    with pytest.raises(AppError):
        V15AccessService.favorite_resource(chunk, "user", "image", None)
    with pytest.raises(AppError):
        V15AccessService.current_resource(detail(), "user", "whole", "stale")


def test_projection_rejects_forged_reference_even_when_event_id_matches():
    event = FavoriteResourceEvent(
        event_id="event",
        sequence=1,
        favorite_id="favorite",
        snapshot_revision_id="snapshot",
        active=True,
        content_type="dialogue",
        content_id="scene",
        revision_id="revision",
        image_asset_id="image",
    )
    V15AccessService.validate_projection_event(event.model_dump(), event)
    for key, value in [
        ("image_asset_id", "other-image"),
        ("content_id", "other-scene"),
        ("snapshot_revision_id", "other-snapshot"),
        ("sequence", 2),
        ("active", False),
    ]:
        with pytest.raises(AppError):
            V15AccessService.validate_projection_event({**event.model_dump(), key: value}, event)


@pytest.mark.asyncio
async def test_unknown_legacy_content_version_is_not_zero_and_never_guesses_locator():
    svc = service(AccessFacts(False, False, 1, "unknown"))
    legacy = replace(saved(), revision_id=None, saved_content_version=None)
    svc.favorite_authority.return_value = legacy
    result = await svc.source_resolution("user", "favorite", NOW)
    assert result.saved_content_version is None
    assert result.target_content_version is None and result.locator is None
    assert result.status == "NO_ACCESS"
    with pytest.raises(AppError):
        await svc.review_resource("user", "favorite", "word-audio", "word-v1", NOW)
