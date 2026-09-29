from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactCorrectionPage,
    ContactProjection,
    ContactTimelineEvent,
    CorrectionDecision,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 1, 0, tzinfo=UTC)


class RecordingAuditRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self.fail = fail

    async def append(self, event: AuditEvent) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        return self.events[-limit:]


class FakeMiniappClient:
    def __init__(self) -> None:
        self.fail_update = False
        self.calls: list[tuple[object, ...]] = []
        self.correction = ContactCorrection(
            id="correction-1",
            user_id="user-1",
            juya_number="JY000000000001",
            nickname="学习者",
            wechat_id="wx-private",
            reason="微信号需要更正",
            status="PENDING",
            created_at=NOW,
            processed_at=None,
            timeline=(
                ContactTimelineEvent(
                    "PENDING",
                    "USER",
                    "user-1",
                    "CONTACT_CORRECTION_CREATED",
                    NOW,
                ),
            ),
        )

    async def list_contact_corrections(
        self, status: str | None, page: int, page_size: int, admin_id: str
    ) -> ContactCorrectionPage:
        self.calls.append(("list", status, page, page_size, admin_id))
        return ContactCorrectionPage((self.correction,), 1, page, page_size)

    async def get_contact_correction(self, correction_id: str, admin_id: str) -> ContactCorrection:
        self.calls.append(("detail", correction_id, admin_id))
        return self.correction

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection:
        self.calls.append(("status", user_id, status, admin_id))
        if self.fail_update:
            raise AppError("MINIAPP_API_UNAVAILABLE", "unavailable", 503)
        return ContactProjection(user_id, "wx-private", status, False, None, None, NOW)

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
        self.calls.append(("verify", user_id, admin_id))
        return ContactProjection(user_id, "wx-private", "PENDING", False, NOW, admin_id, NOW)

    async def decide_contact_correction(
        self,
        correction_id: str,
        decision: str,
        admin_id: str,
        idempotency_key: str,
    ) -> CorrectionDecision:
        self.calls.append(("decision", correction_id, decision, admin_id, idempotency_key))
        return CorrectionDecision(correction_id, decision, NOW)


@pytest.mark.asyncio
async def test_successful_contact_actions_write_only_redacted_audits() -> None:
    from juya_admin_api.modules.contacts.service import ContactAdminService

    audit_repository = RecordingAuditRepository()
    client = FakeMiniappClient()
    service = ContactAdminService(client, AuditService(audit_repository))

    await service.list_corrections("PENDING", 1, 20, "admin-1", "request-list", NOW)
    await service.get_correction("correction-1", "admin-1", "request-detail", NOW)
    await service.update_status("user-1", "CONTACTED", "admin-1", "request-status", NOW)
    await service.verify_change("user-1", "admin-1", "request-verify", NOW)
    await service.decide_correction(
        "correction-1",
        "APPROVED",
        "admin-1",
        "idem-1",
        "request-decision",
        NOW,
    )
    await service.audit_copy("user-1", "admin-1", "request-copy", NOW)

    assert [event.action for event in audit_repository.events] == [
        "contact.view",
        "contact.view",
        "contact.status.update",
        "contact.change.verify",
        "contact.correction.approve",
        "contact.copy",
    ]
    serialized = repr(audit_repository.events)
    assert "wx-private" not in serialized
    assert "微信号需要更正" not in serialized
    assert audit_repository.events[2].after_summary == {"status": "CONTACTED"}


@pytest.mark.asyncio
async def test_upstream_failure_does_not_write_success_audit() -> None:
    from juya_admin_api.modules.contacts.service import ContactAdminService

    audit_repository = RecordingAuditRepository()
    client = FakeMiniappClient()
    client.fail_update = True
    service = ContactAdminService(client, AuditService(audit_repository))

    with pytest.raises(AppError):
        await service.update_status("user-1", "CONTACTED", "admin-1", "request-status", NOW)

    assert audit_repository.events == []


@pytest.mark.asyncio
async def test_copy_is_not_reported_successful_when_audit_storage_fails() -> None:
    from juya_admin_api.modules.contacts.service import ContactAdminService

    service = ContactAdminService(
        FakeMiniappClient(),
        AuditService(RecordingAuditRepository(fail=True)),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.audit_copy("user-1", "admin-1", "request-copy", NOW)
