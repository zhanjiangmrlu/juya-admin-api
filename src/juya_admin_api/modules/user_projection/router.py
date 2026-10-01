from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.integrations.miniapp_api.client import ContactProjection
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.analytics.service import (
    AnalyticsRepository,
    export_aggregate_rows,
)
from juya_admin_api.modules.dashboard.service import DashboardService
from juya_admin_api.modules.user_projection.deletion_service import DeletionCleanupService
from juya_admin_api.modules.user_projection.service import (
    UserListItem,
    UserProjection,
    UserProjectionService,
)
from juya_admin_api.modules.work_items.service import WorkItemService


class WechatSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wechat_id: str = Field(min_length=1, max_length=64)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    contact_status: str | None = None
    entitlement_type: Literal["FORMAL", "LIMITED"] | None = None
    entitlement_status: str | None = None
    profile_completeness: Literal["COMPLETE", "INCOMPLETE"] | None = None
    cohort: Literal["NEW_TODAY", "OPEN_WITHOUT_CONTACT"] | None = None


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)


class AccountDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=26, max_length=26)
    deletion_request_id: str = Field(min_length=26, max_length=26)
    event_id: str = Field(min_length=26, max_length=26)


AdminDependency = Callable[..., Awaitable[SessionRecord]]
ContactStatus = Literal[
    "NOT_PROVIDED",
    "PENDING",
    "CONTACTED",
    "UNREACHABLE",
    "DO_NOT_CONTACT",
]


class UserContactResponse(BaseModel):
    wechat_id: str | None
    contact_status: ContactStatus
    change_pending: bool
    verified_at: datetime | None
    verified_by: str | None
    updated_at: datetime


class UserProjectionResponse(BaseModel):
    user_id: str
    account_status: str
    last_active_at: datetime | None
    formal_entitlement_count: int
    limited_entitlement_count: int
    open_feedback_count: int
    contact: UserContactResponse | None
    contact_degraded: bool
    juya_number: str = ""
    nickname: str | None = None
    avatar_object_key: str | None = None
    avatar_url: str | None = None
    open_scene_completed_count: int | None = 0
    change_pending: bool = False
    contact_changed_at: datetime | None = None


class UserDetailResponse(UserProjectionResponse):
    learning_degraded: bool
    open_scene_completed_count: int | None
    learning_days: int | None
    favorite_count: int | None
    records: dict[str, object] = Field(default_factory=dict)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def create_operations_router(
    users: UserProjectionService,
    dashboard: DashboardService,
    work_items: WorkItemService,
    analytics: AnalyticsRepository,
    *,
    current_admin: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-operations"])

    @router.get("/users", response_model=list[UserProjectionResponse])
    async def search_users(
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
        query: Annotated[str | None, Query(max_length=64)] = None,
        contact_status: Annotated[ContactStatus | None, Query()] = None,
        entitlement_type: Literal["FORMAL", "LIMITED"] | None = None,
        entitlement_status: Literal[
            "ACTIVE", "PAUSED", "REVOKED", "PENDING", "ENDED", "START_EXPIRED", "EXPIRED"
        ]
        | None = None,
        profile_completeness: Literal["COMPLETE", "INCOMPLETE"] | None = None,
        cohort: Literal["NEW_TODAY", "OPEN_WITHOUT_CONTACT"] | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[dict[str, object]]:
        _no_store(response)
        items = await users.search(
            query,
            contact_status=contact_status,
            entitlement_type=entitlement_type,
            entitlement_status=entitlement_status,
            profile_completeness=profile_completeness,
            cohort=cohort,
            page=page,
            page_size=page_size,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        return [_list_item_body(item) for item in items]

    @router.post("/users/search-by-wechat", response_model=list[UserProjectionResponse])
    async def search_users_by_wechat(
        payload: WechatSearchRequest,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> list[dict[str, object]]:
        _no_store(response)
        items = await users.search(
            wechat_id=payload.wechat_id,
            contact_status=payload.contact_status,
            entitlement_type=payload.entitlement_type,
            entitlement_status=payload.entitlement_status,
            profile_completeness=payload.profile_completeness,
            cohort=payload.cohort,
            page=payload.page,
            page_size=payload.page_size,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        return [_list_item_body(item) for item in items]

    @router.get("/users/{user_id}", response_model=UserDetailResponse)
    async def user_detail(
        user_id: str,
        request: Request,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        _no_store(response)
        detail = await users.detail(
            user_id,
            admin_id=str(admin.admin_user_id),
            request_id=_request_id(request),
            occurred_at=clock(),
        )
        body = _projection_body(detail.projection)
        body["contact_degraded"] = detail.contact_degraded
        body["contact"] = _contact_body(detail.contact)
        body["learning_degraded"] = detail.learning_degraded
        learning = detail.learning_overview
        body["open_scene_completed_count"] = (
            None if learning is None else learning.open_scene_completed_count
        )
        body["learning_days"] = None if learning is None else learning.learning_days
        body["favorite_count"] = None if learning is None else learning.favorite_count
        body["records"] = detail.records
        return body

    @router.get("/dashboard")
    async def dashboard_snapshot(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, int]:
        snapshot = await dashboard.get_snapshot()
        return asdict(snapshot)

    @router.get("/work-items")
    async def active_work_items(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> list[dict[str, object]]:
        return [
            {
                "key": item.key,
                "kind": item.kind,
                "priority_rank": item.priority_rank,
                "due_at": item.due_at,
            }
            for item in await work_items.active(clock())
        ]

    @router.get("/analytics/export")
    async def analytics_export(
        start: date,
        end: date,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> tuple[dict[str, object], ...]:
        return export_aggregate_rows(await analytics.query(start, end))

    return router


def create_internal_deletion_router(
    service: DeletionCleanupService,
    *,
    current_service: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal-users"])

    @router.post("/account-deletions")
    async def cleanup_account(
        payload: AccountDeletionRequest,
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        result = await service.cleanup(
            payload.event_id,
            payload.user_id,
            clock(),
            deletion_request_id=payload.deletion_request_id,
        )
        return {
            "event_id": result.event_id,
            "user_id": result.user_id,
            "status": result.status,
            "completed_at": result.completed_at,
        }

    @router.post("/users/{user_id}/deletion")
    async def cleanup_user(
        user_id: str,
        payload: DeletionRequest,
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        result = await service.cleanup(payload.event_id, user_id, clock())
        return {
            "event_id": result.event_id,
            "user_id": result.user_id,
            "status": result.status,
            "completed_at": result.completed_at,
        }

    return router


def _projection_body(projection: UserProjection) -> dict[str, object]:
    return {
        "user_id": projection.user_id,
        "juya_number": projection.juya_number,
        "nickname": projection.nickname,
        "avatar_object_key": projection.avatar_object_key,
        "avatar_url": projection.avatar_url,
        "open_scene_completed_count": projection.open_scene_completed_count,
        "change_pending": projection.change_pending,
        "contact_changed_at": projection.contact_changed_at,
        "account_status": projection.account_status,
        "last_active_at": projection.last_active_at,
        "formal_entitlement_count": projection.formal_entitlement_count,
        "limited_entitlement_count": projection.limited_entitlement_count,
        "open_feedback_count": projection.open_feedback_count,
    }


def _contact_body(contact: ContactProjection | None) -> dict[str, object] | None:
    if contact is None:
        return None
    return {
        "wechat_id": contact.wechat_id,
        "contact_status": contact.contact_status,
        "change_pending": contact.change_pending,
        "verified_at": contact.verified_at,
        "verified_by": contact.verified_by,
        "updated_at": contact.updated_at,
    }


def _list_item_body(item: UserListItem) -> dict[str, object]:
    body = _projection_body(item.projection)
    body["contact"] = _contact_body(item.contact)
    body["contact_degraded"] = item.contact_degraded
    return body
