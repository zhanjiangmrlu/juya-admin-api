from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository


class Session:
    def __init__(self):
        self.statements = []

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, statement, params):
        self.statements.append((str(statement), params))
        return SimpleNamespace(
            first=lambda: SimpleNamespace(id=10, capacity=30, granted_user_count=8)
        )

    async def scalar(self, statement, params):
        return 11


class Sessions:
    def __init__(self, session):
        self.session = session

    @asynccontextmanager
    async def __call__(self):
        yield self.session


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["OPEN", "PAUSED"])
async def test_copy_open_or_paused_keeps_source_snapshot_and_starts_empty_draft(status):
    session = Session()
    repo = SQLAlchemyCampaignRepository(Sessions(session))
    repo._claim_idempotency = AsyncMock(return_value=None)
    repo._lock_campaign = AsyncMock(
        return_value=SimpleNamespace(id=1, current_version_id=10, status=status)
    )
    result = {"id": "campaign", "status": "DRAFT", "current_version": {"id": "new"}}
    repo._get = AsyncMock(side_effect=[{"id": "campaign", "status": status}, result])
    repo._audit = AsyncMock()
    repo._complete_idempotency = AsyncMock()
    assert (
        await repo.command("campaign", "copy", expected_version=3, now=datetime.now(UTC)) == result
    )
    sql = "\n".join(statement for statement, _ in session.statements)
    assert "UPDATE limited_campaign_version" not in sql
    assert "UPDATE limited_entitlement" not in sql
    assert "'DRAFT', duration_days, activation_window_days, capacity, 0" in sql
    assert "FROM limited_campaign_scene WHERE campaign_version_id = :old_id" in sql
    assert "status = 'DRAFT' WHERE id = :id" in sql
    repo._lock_campaign.assert_awaited_once_with(session, "campaign", 3)
