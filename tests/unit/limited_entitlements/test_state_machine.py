from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.campaigns.domain import CampaignVersion
from juya_admin_api.modules.limited_entitlements.domain import RemedyMode
from juya_admin_api.modules.limited_entitlements.repository import (
    InMemoryLimitedEntitlementRepository,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)


def make_service(
    *, capacity: int = 10
) -> tuple[LimitedEntitlementService, InMemoryLimitedEntitlementRepository]:
    repository = InMemoryLimitedEntitlementRepository()
    repository.campaign_versions["campaign-version-1"] = CampaignVersion(
        id="campaign-version-1",
        campaign_id="campaign-1",
        status="OPEN",
        duration_days=3,
        activation_window_days=7,
        capacity=capacity,
        granted_user_count=0,
        scene_ids=("scene-1", "scene-2"),
    )
    return LimitedEntitlementService(repository), repository


@pytest.mark.asyncio
async def test_pending_activates_once_and_expires_at_exact_boundary() -> None:
    service, _ = make_service()
    entitlement = await service.grant("user-1", "campaign-version-1", "admin-1", "grant-1", NOW)

    first = await service.activate_for_scene("user-1", "scene-1", NOW)
    second = await service.activate_for_scene("user-1", "scene-1", NOW + timedelta(hours=1))

    assert entitlement.status == "ACTIVE"
    assert first.activated_at == NOW
    assert second.activated_at == NOW
    assert first.earliest_expires_at == NOW + timedelta(days=3)

    expired = await service.activate_for_scene("user-1", "scene-1", NOW + timedelta(days=3))

    assert entitlement.status == "ENDED"
    assert expired.level.value == "HIDDEN"


@pytest.mark.asyncio
async def test_start_window_expires_and_one_remedy_restores_pending() -> None:
    service, _ = make_service()
    entitlement = await service.grant("user-1", "campaign-version-1", "admin-1", "grant-1", NOW)
    after_window = NOW + timedelta(days=7)

    denied = await service.activate_for_scene("user-1", "scene-1", after_window)
    assert denied.level.value == "HIDDEN"
    assert entitlement.status == "START_EXPIRED"

    restored = await service.remedy(
        entitlement.id,
        RemedyMode.RESTORE_START_WINDOW,
        "admin-1",
        "remedy-1",
        after_window,
    )
    assert restored.status == "PENDING"
    assert restored.start_deadline == after_window + timedelta(days=7)

    with pytest.raises(AppError) as exc:
        await service.remedy(
            entitlement.id,
            RemedyMode.EXTEND_START_DEADLINE,
            "admin-1",
            "remedy-2",
            after_window,
        )
    assert exc.value.code == "LIMITED_REMEDY_ALREADY_USED"


@pytest.mark.asyncio
async def test_active_cannot_be_extended_and_pause_resume_adds_no_time() -> None:
    service, _ = make_service()
    entitlement = await service.grant("user-1", "campaign-version-1", "admin-1", "grant-1", NOW)
    await service.activate_for_scene("user-1", "scene-1", NOW)
    original_expiry = entitlement.expires_at

    with pytest.raises(AppError) as exc:
        await service.remedy(
            entitlement.id,
            RemedyMode.EXTEND_START_DEADLINE,
            "admin-1",
            "remedy-active",
            NOW,
        )
    assert exc.value.code == "LIMITED_ACTIVE_CANNOT_EXTEND"

    await service.pause(entitlement.id, "admin-1", "pause-1", "incident", NOW)
    resumed = await service.resume(entitlement.id, "admin-1", "resume-1", NOW + timedelta(days=1))
    assert resumed.status == "ACTIVE"
    assert resumed.expires_at == original_expiry


@pytest.mark.asyncio
async def test_capacity_count_never_decreases_after_revoke() -> None:
    service, repository = make_service(capacity=1)
    entitlement = await service.grant("user-1", "campaign-version-1", "admin-1", "grant-1", NOW)
    await service.revoke(entitlement.id, "admin-1", "revoke-1", "cancel", NOW)

    with pytest.raises(AppError) as exc:
        await service.grant("user-2", "campaign-version-1", "admin-1", "grant-2", NOW)
    assert exc.value.code == "CAMPAIGN_CAPACITY_REACHED"
    assert repository.campaign_versions["campaign-version-1"].granted_user_count == 1
