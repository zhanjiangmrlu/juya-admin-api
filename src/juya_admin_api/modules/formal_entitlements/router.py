from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService


class EntitlementCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    package_id: str = Field(min_length=1, max_length=64)
    term: EntitlementTerm | None = None
    reason: str | None = Field(default=None, max_length=500)


AdminDependency = Callable[..., Awaitable[SessionRecord]]


def _serialize(entitlement: FormalEntitlement) -> dict[str, object]:
    return {
        "id": entitlement.id,
        "user_id": entitlement.user_id,
        "package_id": entitlement.package_id,
        "status": entitlement.status,
        "term": entitlement.term,
        "granted_at": entitlement.granted_at,
        "expires_at": entitlement.expires_at,
        "version": entitlement.version,
    }


def create_formal_entitlement_router(
    service: FormalEntitlementService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/formal-entitlements",
        tags=["formal-entitlements"],
    )

    @router.post("/preview-operation")
    async def preview_operation(
        payload: EntitlementCommandRequest,
        operation: EntitlementOperation,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        command = FormalEntitlementCommand(
            payload.user_id,
            payload.package_id,
            operation,
            payload.term,
            payload.reason,
        )
        return _serialize(await service.preview_operation(command, clock()))

    async def apply(
        operation: EntitlementOperation,
        payload: EntitlementCommandRequest,
        admin: SessionRecord,
        idempotency_key: str,
    ) -> dict[str, object]:
        command = FormalEntitlementCommand(
            payload.user_id,
            payload.package_id,
            operation,
            payload.term,
            payload.reason,
        )
        result = await service.apply_operation(
            command,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/commands/{operation}")
    async def apply_operation(
        operation: EntitlementOperation,
        payload: EntitlementCommandRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        return await apply(operation, payload, admin, idempotency_key)

    return router
