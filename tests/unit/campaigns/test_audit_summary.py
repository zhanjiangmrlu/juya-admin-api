import json
from datetime import UTC, datetime
from typing import Any

import pytest

from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository


@pytest.mark.asyncio
async def test_campaign_audit_summary_omits_names_and_unbounded_scene_ids() -> None:
    class Session:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        async def execute(self, _statement: object, params: dict[str, Any]) -> None:
            self.params = params

    session = Session()
    repository = SQLAlchemyCampaignRepository(None)  # type: ignore[arg-type]
    after = {
        "id": "campaign-1",
        "name": "private campaign name",
        "status": "OPEN",
        "version": 2,
        "current_version": {
            "id": "version-1",
            "capacity": 10,
            "granted_user_count": 1,
            "scene_ids": ["scene-" + str(i) for i in range(1000)],
        },
    }
    await repository._audit(
        session, 1, "OPEN", None, after, "admin-1", datetime(2026, 9, 29, tzinfo=UTC)
    )  # type: ignore[arg-type]
    summary = json.loads(session.params["after"])
    assert summary == {
        "id": "campaign-1",
        "status": "OPEN",
        "version": 2,
        "current_version_id": "version-1",
        "capacity": 10,
        "granted_user_count": 1,
    }
