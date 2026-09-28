from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.campaigns.domain import CampaignVersion
from juya_admin_api.modules.campaigns.service import CampaignService
from juya_admin_api.shared.errors import AppError


def test_campaign_revision_validates_and_protects_locked_terms() -> None:
    version = CampaignVersion("v1", "c1", "DRAFT", 3, 7, 10, 2, ("scene-1",))

    revised = CampaignService.revise_version(
        version,
        duration_days=5,
        activation_window_days=3,
        capacity=20,
        scene_ids=("scene-1", "scene-2"),
    )
    assert (revised.duration_days, revised.capacity) == (5, 20)

    revised.locked_at = datetime(2026, 9, 29, tzinfo=UTC)
    with pytest.raises(AppError) as locked:
        CampaignService.revise_version(
            revised,
            duration_days=3,
            activation_window_days=3,
            capacity=20,
            scene_ids=revised.scene_ids,
        )
    assert locked.value.code == "CAMPAIGN_VERSION_LOCKED"
