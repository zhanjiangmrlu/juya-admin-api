from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.analytics.service import (
    AnalyticsRepository,
    export_aggregate_rows,
)
from juya_admin_api.modules.dashboard.service import DashboardService
from juya_admin_api.modules.user_projection.deletion_service import DeletionCleanupService
from juya_admin_api.modules.user_projection.service import UserProjection, UserProjectionService
from juya_admin_api.modules.work_items.service import WorkItemService


class WechatSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wechat_id: str = Field(min_length=1, max_length=64)


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)


AdminDependency = Callable[..., Awaitable[SessionRecord]]


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

    @router.get("/users")
    async def search_users(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        query: Annotated[str | None, Query(max_length=64)] = None,
    ) -> list[dict[str, object]]:
        return [_projection_body(item) for item in await users.search(query)]

    @router.post("/users/search-by-wechat")
    async def search_users_by_wechat(
        payload: WechatSearchRequest,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> list[dict[str, object]]:
        return [_projection_body(item) for item in await users.search(wechat_id=payload.wechat_id)]

    @router.get("/users/{user_id}")
    async def user_detail(
        user_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        detail = await users.detail(user_id)
        body = _projection_body(detail.projection)
        body["contact_degraded"] = detail.contact_degraded
        body["contact"] = (
            None
            if detail.contact is None
            else {
                "wechat_id": detail.contact.wechat_id,
                "contact_status": detail.contact.contact_status,
            }
        )
        return body

    @router.get("/dashboard")
    async def dashboard_snapshot(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, int]:
        snapshot = await dashboard.get_snapshot()
        return {
            "active_users": snapshot.active_users,
            "open_feedback": snapshot.open_feedback,
            "overdue_feedback": snapshot.overdue_feedback,
            "expiring_entitlements": snapshot.expiring_entitlements,
            "failed_jobs": snapshot.failed_jobs,
        }

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
    router = APIRouter(prefix="/internal/v1/users", tags=["internal-users"])

    @router.post("/{user_id}/deletion")
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
        "account_status": projection.account_status,
        "last_active_at": projection.last_active_at,
        "formal_entitlement_count": projection.formal_entitlement_count,
        "limited_entitlement_count": projection.limited_entitlement_count,
        "open_feedback_count": projection.open_feedback_count,
    }
