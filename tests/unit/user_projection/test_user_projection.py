from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.miniapp_api.client import (
    ContactProjection,
    ContactProjectionResult,
    LearningOverview,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.user_projection.service import (
    InMemoryUserProjectionRepository,
    UserProjection,
    UserProjectionService,
)
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)


class RecordingAuditRepository:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        return self.events[-limit:]


class FakeMiniappClient:
    def __init__(self) -> None:
        self.contact_degraded = False
        self.learning_unavailable = False
        self.calls: list[tuple[object, ...]] = []
        self.contacts = (
            ContactProjection("user-1", "wx-private", "CONTACTED", False, NOW, "admin-0", NOW),
            ContactProjection("user-2", None, "PENDING", True, None, None, NOW),
        )

    async def search_user_ids_by_wechat(
        self, wechat_id: str, admin_id: str = "system"
    ) -> tuple[str, ...]:
        self.calls.append(("wechat", wechat_id, admin_id))
        return ("user-1",)

    async def get_contact_projections(
        self, user_ids: tuple[str, ...], admin_id: str = "system"
    ) -> ContactProjectionResult:
        self.calls.append(("contacts", user_ids, admin_id))
        if self.contact_degraded:
            return ContactProjectionResult((), True)
        allowed = frozenset(user_ids)
        return ContactProjectionResult(
            tuple(item for item in self.contacts if item.user_id in allowed), False
        )

    async def get_learning_overview(self, user_id: str, admin_id: str) -> LearningOverview:
        self.calls.append(("learning", user_id, admin_id))
        if self.learning_unavailable:
            raise AppError("MINIAPP_API_UNAVAILABLE", "unavailable", 503)
        return LearningOverview(7, 12, 3)


def _service() -> tuple[
    UserProjectionService,
    FakeMiniappClient,
    RecordingAuditRepository,
]:
    repository = InMemoryUserProjectionRepository()
    repository.users = {
        "user-1": UserProjection("user-1", "ACTIVE", NOW, 1, 2, 0),
        "user-2": UserProjection("user-2", "ACTIVE", NOW, 0, 1, 1),
    }
    client = FakeMiniappClient()
    audits = RecordingAuditRepository()
    return UserProjectionService(repository, client, AuditService(audits)), client, audits


@pytest.mark.asyncio
async def test_search_batches_contacts_filters_server_side_and_audits_sensitive_rows() -> None:
    service, client, audits = _service()

    result = await service.search(
        contact_status="CONTACTED",
        admin_id="admin-1",
        request_id="request-list",
        occurred_at=NOW,
    )

    assert [item.projection.user_id for item in result] == ["user-1"]
    assert result[0].contact == client.contacts[0]
    assert client.calls == [("contacts", ("user-1", "user-2"), "admin-1")]
    assert [event.action for event in audits.events] == ["contact.view.list"]
    assert audits.events[0].after_summary == {
        "hit_count": 1,
        "user_ids": ["user-1"],
    }
    assert "wx-private" not in repr(audits.events)


@pytest.mark.asyncio
async def test_search_by_wechat_uses_admin_call_and_keeps_local_order() -> None:
    service, client, _audits = _service()

    result = await service.search(
        wechat_id="wx-private",
        admin_id="admin-1",
        request_id="request-search",
        occurred_at=NOW,
    )

    assert [item.projection.user_id for item in result] == ["user-1"]
    assert client.calls[:2] == [
        ("wechat", "wx-private", "admin-1"),
        ("contacts", ("user-1",), "admin-1"),
    ]


@pytest.mark.asyncio
async def test_unfiltered_search_degrades_without_fabricating_contacts() -> None:
    service, client, audits = _service()
    client.contact_degraded = True

    result = await service.search(
        admin_id="admin-1",
        request_id="request-list",
        occurred_at=NOW,
    )

    assert len(result) == 2
    assert all(item.contact is None and item.contact_degraded for item in result)
    assert audits.events == []

    with pytest.raises(AppError, match="联系资料") as captured:
        await service.search(
            contact_status="CONTACTED",
            admin_id="admin-1",
            request_id="request-filter",
            occurred_at=NOW,
        )
    assert captured.value.code == "MINIAPP_API_UNAVAILABLE"


@pytest.mark.asyncio
async def test_detail_aggregates_contact_and_learning_and_audits_after_success() -> None:
    service, client, audits = _service()

    detail = await service.detail(
        "user-1",
        admin_id="admin-1",
        request_id="request-detail",
        occurred_at=NOW,
    )

    assert detail.contact == client.contacts[0]
    assert detail.contact_degraded is False
    assert detail.learning_overview == LearningOverview(7, 12, 3)
    assert detail.learning_degraded is False
    assert [event.action for event in audits.events] == ["contact.view.detail"]
    assert audits.events[0].after_summary == {"hit_count": 1, "user_id": "user-1"}
    assert "wx-private" not in repr(audits.events)


@pytest.mark.asyncio
async def test_detail_learning_unavailable_is_explicitly_degraded() -> None:
    service, client, _audits = _service()
    client.learning_unavailable = True

    detail = await service.detail(
        "user-1",
        admin_id="admin-1",
        request_id="request-detail",
        occurred_at=NOW,
    )

    assert detail.learning_overview is None
    assert detail.learning_degraded is True
