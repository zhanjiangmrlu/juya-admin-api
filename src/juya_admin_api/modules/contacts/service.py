from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactCorrectionPage,
    ContactProjection,
    CorrectionDecision,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

CONTACT_STATUSES = frozenset(
    {"NOT_PROVIDED", "PENDING", "CONTACTED", "UNREACHABLE", "DO_NOT_CONTACT"}
)


class ContactClient(Protocol):
    async def list_contact_corrections(
        self, status: str | None, page: int, page_size: int, admin_id: str
    ) -> ContactCorrectionPage: ...

    async def get_contact_correction(
        self, correction_id: str, admin_id: str
    ) -> ContactCorrection: ...

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection: ...

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection: ...

    async def decide_contact_correction(
        self,
        correction_id: str,
        decision: str,
        admin_id: str,
        idempotency_key: str,
    ) -> CorrectionDecision: ...


class ContactAdminService:
    def __init__(self, client: ContactClient, audit: AuditService) -> None:
        self._client = client
        self._audit = audit

    async def list_corrections(
        self,
        status: str | None,
        page: int,
        page_size: int,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactCorrectionPage:
        result = await self._client.list_contact_corrections(status, page, page_size, actor_id)
        await self._record(
            actor_id,
            "contact.view",
            "contact_correction_list",
            "list",
            request_id,
            now,
            after_summary={"count": len(result.items), "status": status or "ALL"},
        )
        return result

    async def get_correction(
        self,
        correction_id: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactCorrection:
        result = await self._client.get_contact_correction(correction_id, actor_id)
        await self._record(
            actor_id,
            "contact.view",
            "contact_correction",
            correction_id,
            request_id,
            now,
            after_summary={"status": result.status, "user_id": result.user_id},
        )
        return result

    async def update_status(
        self,
        user_id: str,
        status: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactProjection:
        if status not in CONTACT_STATUSES:
            raise AppError("CONTACT_STATUS_INVALID", "联系状态无效", 422)
        result = await self._client.update_contact_status(user_id, status, actor_id)
        await self._record(
            actor_id,
            "contact.status.update",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"status": result.contact_status},
        )
        return result

    async def verify_change(
        self,
        user_id: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactProjection:
        result = await self._client.verify_contact_change(user_id, actor_id)
        await self._record(
            actor_id,
            "contact.change.verify",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"change_pending": result.change_pending},
        )
        return result

    async def decide_correction(
        self,
        correction_id: str,
        decision: str,
        actor_id: str,
        idempotency_key: str,
        request_id: str,
        now: datetime,
    ) -> CorrectionDecision:
        normalized = decision.upper()
        action_by_decision = {
            "APPROVED": "contact.correction.approve",
            "REJECTED": "contact.correction.reject",
        }
        if normalized not in action_by_decision:
            raise AppError("CORRECTION_DECISION_INVALID", "更正决定无效", 422)
        result = await self._client.decide_contact_correction(
            correction_id, normalized, actor_id, idempotency_key
        )
        await self._record(
            actor_id,
            action_by_decision[normalized],
            "contact_correction",
            correction_id,
            request_id,
            now,
            after_summary={"status": result.status},
        )
        return result

    async def audit_copy(self, user_id: str, actor_id: str, request_id: str, now: datetime) -> None:
        await self._record(
            actor_id,
            "contact.copy",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"copied": True},
        )

    async def _record(
        self,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        request_id: str,
        now: datetime,
        *,
        after_summary: dict[str, object],
    ) -> None:
        await self._audit.record(
            AuditEvent(
                actor_public_id=actor_id,
                action=action,
                object_type=object_type,
                object_public_id=object_id,
                before_summary={},
                after_summary=after_summary,
                reason=None,
                request_id=request_id,
                occurred_at=now,
            )
        )
