from datetime import timedelta

import pytest

from juya_admin_api.shared.errors import AppError
from tests.unit.limited_entitlements.test_state_machine import NOW, make_service


@pytest.mark.asyncio
async def test_grant_preview_is_read_only_and_detects_capacity_change() -> None:
    service, repository = make_service()
    preview = await service.preview_grant("user-1", "campaign-version-1", NOW)
    assert preview["before"] is None
    assert preview["after"]["status"] == "PENDING"
    assert preview["after"]["start_deadline"] == NOW + timedelta(days=7)
    assert preview["after"]["expires_at"] is None
    assert not repository.entitlements
    assert repository.campaign_versions["campaign-version-1"].granted_user_count == 0
    repository.campaign_versions["campaign-version-1"].capacity = 11
    with pytest.raises(AppError) as failure:
        await service.grant(
            "user-1",
            "campaign-version-1",
            "admin-1",
            "key",
            NOW,
            expected_fingerprint=preview["expected_fingerprint"],
        )
    assert failure.value.code == "CAMPAIGN_PREVIEW_CONFLICT"
    assert not repository.entitlements


@pytest.mark.asyncio
async def test_prepared_grant_retry_replays_even_after_snapshot_changes() -> None:
    service, repository = make_service(capacity=1)
    preview = await service.preview_grant("user-1", "campaign-version-1", NOW)
    first = await service.grant(
        "user-1",
        "campaign-version-1",
        "admin-1",
        "key",
        NOW,
        expected_fingerprint=preview["expected_fingerprint"],
    )
    again = await service.grant(
        "user-1",
        "campaign-version-1",
        "admin-1",
        "key",
        NOW + timedelta(hours=1),
        expected_fingerprint=preview["expected_fingerprint"],
    )
    assert first == again
    assert len(repository.entitlements) == 1
    assert repository.campaign_versions["campaign-version-1"].granted_user_count == 1
