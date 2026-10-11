"""Additive internal V1.5 routes, authenticated by the existing body-bound HMAC dependency."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.content.v15_contracts import (
    ContentType,
    FavoriteReference,
    FavoriteResourceEvent,
)
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.shared.errors import AppError

ServiceDependency = Callable[..., Awaitable[ServicePrincipal]]


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)


class ResourceQuery(UserRequest):
    resource_version: str | None = None


class ConfirmationQuery(UserRequest):
    reference: FavoriteReference


def create_v15_access_router(
    service: V15AccessService,
    *,
    current_service: ServiceDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    bff_service_name: str = "juya-miniapp-api",
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal-v15-access"])

    def restrict(principal: ServicePrincipal, response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"
        if principal.service_name != bff_service_name:
            raise AppError("SERVICE_PURPOSE_DENIED", "当前服务无权调用用户内容委托接口", 403)

    @router.post("/content/{content_type}/{content_id}/open")
    async def open_content(
        content_type: ContentType,
        content_id: str,
        payload: UserRequest,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.open(payload.user_id, content_type, content_id, clock())

    @router.post("/content/{content_type}/{content_id}/preview")
    async def preview(
        content_type: ContentType,
        content_id: str,
        payload: UserRequest,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.preview(payload.user_id, content_type, content_id, clock())

    @router.post("/content/{content_type}/{content_id}/resources/{target_id}/signed-url")
    async def resource(
        content_type: ContentType,
        content_id: str,
        target_id: str,
        payload: ResourceQuery,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.content_resource(
            payload.user_id, content_type, content_id, target_id, payload.resource_version, clock()
        )

    @router.post("/favorite-snapshots/confirmation")
    async def confirmation(
        payload: ConfirmationQuery,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.confirmation(payload.user_id, payload.reference, clock())

    @router.post("/favorite-snapshots/{favorite_id}/source-resolution")
    async def source(
        favorite_id: str,
        payload: UserRequest,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return (await service.source_resolution(payload.user_id, favorite_id, clock())).model_dump(
            mode="json"
        )

    @router.post("/favorite-snapshots/{favorite_id}/resources/{target_id}/signed-url")
    async def review(
        favorite_id: str,
        target_id: str,
        payload: ResourceQuery,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.review_resource(
            payload.user_id, favorite_id, target_id, payload.resource_version, clock()
        )

    @router.post("/favorite-resource-projections")
    async def projection(
        payload: FavoriteResourceEvent,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        restrict(principal, response)
        return await service.project_event(payload)

    return router
