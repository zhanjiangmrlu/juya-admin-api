"""Admin CAS commands and HMAC-authenticated BFF configuration read."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.learning_features.store import LearningFeaturesStore
from juya_admin_api.shared.errors import AppError


class FeaturesResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    recording_enabled: bool = False
    profile_completion_enabled: bool = False
    version: int = Field(ge=0)


class FeaturesCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recording_enabled: bool
    profile_completion_enabled: bool = False
    expected_version: int = Field(ge=0)


class FeaturesRead(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_learning_features_router(
    store: LearningFeaturesStore,
    *,
    current_admin: Callable[..., Awaitable[SessionRecord]],
    current_admin_write: Callable[..., Awaitable[SessionRecord]],
    current_service: Callable[..., Awaitable[ServicePrincipal]],
    bff_service_name: str = "juya-miniapp-api",
) -> APIRouter:
    router = APIRouter(tags=["learning-features"])

    @router.get("/api/v1/admin/learning-features", response_model=FeaturesResponse)
    async def read(admin: Annotated[SessionRecord, Depends(current_admin)]) -> dict[str, Any]:
        return await store.read()

    @router.put("/api/v1/admin/learning-features", response_model=FeaturesResponse)
    async def update(
        payload: FeaturesCommand,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            str(admin.admin_user_id), key, payload.model_dump(exclude_unset=True)
        )

    @router.post("/internal/v1/learning-features", response_model=FeaturesResponse)
    async def internal(
        payload: FeaturesRead,
        response: Response,
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        if principal.service_name != bff_service_name:
            raise AppError("SERVICE_PURPOSE_DENIED", "当前服务无权读取学习审核配置", 403)
        return await store.read()

    return router
