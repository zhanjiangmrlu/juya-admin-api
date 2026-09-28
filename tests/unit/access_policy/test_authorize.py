from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.access_policy.domain import AccessGrant, AccessLevel
from juya_admin_api.modules.access_policy.router import serialize_preview_scene
from juya_admin_api.modules.access_policy.service import AccessPolicyService

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeContentAccess:
    def __init__(self, *, opened: bool = False, preview: bool = False) -> None:
        self.opened = opened
        self.preview = preview

    async def is_open(self, scene_id: str, now: datetime) -> bool:
        return self.opened

    async def is_preview(self, scene_id: str, now: datetime) -> bool:
        return self.preview


class FakeGrantPort:
    def __init__(self, grants: tuple[AccessGrant, ...] = ()) -> None:
        self.grants = grants

    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]:
        return self.grants


@pytest.mark.asyncio
async def test_authorization_is_union_of_all_full_access_sources() -> None:
    expiry = NOW + timedelta(days=10)
    service = AccessPolicyService(
        FakeContentAccess(opened=True, preview=True),
        FakeGrantPort((AccessGrant("formal-1", expiry),)),
        FakeGrantPort((AccessGrant("limited-1", NOW + timedelta(days=3)),)),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.OPEN
    assert decision.sources == ("OPEN", "formal-1", "limited-1")
    assert decision.earliest_expires_at == NOW + timedelta(days=3)


@pytest.mark.asyncio
async def test_expiry_is_exclusive_at_exact_instant() -> None:
    service = AccessPolicyService(
        FakeContentAccess(preview=True),
        FakeGrantPort((AccessGrant("formal-expired", NOW),)),
        FakeGrantPort((AccessGrant("limited-expired", NOW - timedelta(seconds=1)),)),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.PREVIEW
    assert decision.sources == ("PREVIEW",)
    assert decision.earliest_expires_at is None


@pytest.mark.asyncio
async def test_hidden_when_no_full_or_preview_access() -> None:
    service = AccessPolicyService(
        FakeContentAccess(),
        FakeGrantPort(),
        FakeGrantPort(),
    )

    decision = await service.authorize("user-1", "scene-1", NOW)

    assert decision.level is AccessLevel.HIDDEN
    assert decision.sources == ()


def test_preview_serializer_has_an_independent_allowlist() -> None:
    result = serialize_preview_scene(
        {
            "public_id": "scene-1",
            "title": "At the station",
            "series": {"title": "Travel"},
            "cover_url": "https://signed.example/cover",
            "introduction": "Preview",
            "preview_status": "AVAILABLE",
            "entries": [{"secret": "full content"}],
            "internal_notes": "must not leak",
            "oss_object_key": "private/key.jpg",
        }
    )

    assert set(result) == {
        "public_id",
        "title",
        "series",
        "cover_url",
        "introduction",
        "preview_status",
    }
