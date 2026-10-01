from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.modules.user_projection.service import UserProjectionService
from juya_admin_api.shared.errors import AppError


class EntitlementCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    package_id: str = Field(min_length=1, max_length=64)
    term: EntitlementTerm | None = None
    reason: str | None = Field(default=None, max_length=500)


AdminDependency = Callable[..., Awaitable[SessionRecord]]
EntitlementType = Literal["FORMAL", "LIMITED"]
EntitlementStatus = Literal[
    "ACTIVE", "PAUSED", "REVOKED", "PENDING", "ENDED", "START_EXPIRED", "EXPIRED"
]


class EntitlementQueryRepository(Protocol):
    async def list_entitlements(
        self, filters: dict[str, str], page: int, page_size: int
    ) -> dict[str, Any]: ...

    async def list_packages(self, page: int, page_size: int) -> dict[str, Any]: ...

    async def get_formal(self, entitlement_id: str) -> dict[str, Any] | None: ...

    async def get_limited(self, entitlement_id: str) -> dict[str, Any] | None: ...


class EntitlementListItemResponse(BaseModel):
    id: str
    type: EntitlementType
    user_id: str
    status: str
    granted_at: datetime
    expires_at: datetime | None
    package_id: str | None
    campaign_id: str | None

    juya_number: str = ""
    nickname: str | None = None
    wechat_id: str | None = None
    contact_status: str = "NOT_PROVIDED"
    contact_degraded: bool = False
    content_name: str = ""
    campaign_version_id: str | None = None
    campaign_version_no: int | None = None
    term: str | None = None
    effective_at: datetime | None = None
    start_deadline: datetime | None = None


class EntitlementPageResponse(BaseModel):
    items: list[EntitlementListItemResponse]
    page: int
    page_size: int
    total: int


class PackageResponse(BaseModel):
    id: str
    name: str
    status: str
    sort_order: int


class PackagePageResponse(BaseModel):
    items: list[PackageResponse]
    page: int
    page_size: int
    total: int


class FormalEntitlementDetailResponse(BaseModel):
    id: str
    user_id: str
    package_id: str
    package_name: str
    status: str
    term: str
    granted_at: datetime
    expires_at: datetime | None
    version: int
    available_operations: list[str]


def create_entitlement_query_router(
    repository: EntitlementQueryRepository,
    *,
    current_admin: AdminDependency,
    users: UserProjectionService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["entitlements"])

    @router.get("/entitlements", response_model=EntitlementPageResponse)
    async def list_entitlements(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        response: Response,
        user_id: str | None = None,
        type: Annotated[EntitlementType | None, Query()] = None,
        status: Annotated[EntitlementStatus | None, Query()] = None,
        package_id: str | None = None,
        campaign_id: str | None = None,
        campaign_version_id: str | None = None,
        expiry: Literal["EXPIRING", "ENDING", "START_EXPIRING"] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        filters = {
            key: value
            for key, value in {
                "user_id": user_id,
                "type": type,
                "status": status,
                "package_id": package_id,
                "campaign_id": campaign_id,
                "campaign_version_id": campaign_version_id,
                "expiry": expiry,
                "date_from": date_from,
                "date_to": date_to,
            }.items()
            if value is not None
        }
        response.headers["Cache-Control"] = "no-store"
        result = await repository.list_entitlements(filters, page, page_size)
        if users is not None and result["items"]:
            contacts, degraded = await users.contacts_for(
                tuple(dict.fromkeys(item["user_id"] for item in result["items"])),
                admin_id=str(_admin.admin_user_id),
                occurred_at=datetime.now(UTC),
            )
            for item in result["items"]:
                contact = contacts.get(item["user_id"])
                item["wechat_id"] = None if contact is None else contact.wechat_id
                item["contact_degraded"] = degraded
        return result

    @router.get("/content-packages", response_model=PackagePageResponse)
    async def list_packages(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return await repository.list_packages(page, page_size)

    return router


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
    query_repository: EntitlementQueryRepository,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/formal-entitlements",
        tags=["formal-entitlements"],
    )

    @router.get("/{entitlement_id}", response_model=FormalEntitlementDetailResponse)
    async def get_entitlement(
        entitlement_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        result = await query_repository.get_formal(entitlement_id)
        if result is None:
            raise AppError("FORMAL_ENTITLEMENT_NOT_FOUND", "正式权益不存在", 404)
        return result

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
