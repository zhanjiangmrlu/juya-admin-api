from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.access_policy.service import AccessPolicyService
from juya_admin_api.shared.errors import AppError


class InternalContentQueryPort(Protocol):
    async def list_learning_modules(self) -> list[dict[str, object]]: ...

    async def learning_catalog(self, user_id: str) -> list[dict[str, object]]: ...

    async def get_full_scene(self, scene_id: str) -> dict[str, object] | None: ...

    async def get_preview_scene(self, scene_id: str) -> dict[str, object] | None: ...

    async def get_entry(self, scene_id: str, entry_id: str) -> dict[str, object] | None: ...


class SceneActivationPort(Protocol):
    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> AccessDecision: ...


class AccessBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    scene_ids: list[str] = Field(max_length=100)


class UserQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)


ServiceDependency = Callable[..., Awaitable[ServicePrincipal]]
PREVIEW_FIELDS = frozenset(
    {
        "public_id",
        "title",
        "series",
        "cover_url",
        "introduction",
        "preview_status",
    }
)


def serialize_preview_scene(scene: dict[str, object]) -> dict[str, object]:
    return {key: scene[key] for key in PREVIEW_FIELDS if key in scene}


def create_internal_content_router(
    access_policy: AccessPolicyService,
    content_queries: InternalContentQueryPort,
    *,
    current_service: ServiceDependency,
    scene_activation: SceneActivationPort | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1", tags=["internal-content"])

    @router.get("/learning/modules")
    async def learning_modules(
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        return {"items": await content_queries.list_learning_modules()}

    @router.post("/learning/catalog")
    async def learning_catalog(
        payload: UserQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        return {"items": await content_queries.learning_catalog(payload.user_id)}

    @router.post("/access/batch")
    async def access_batch(
        payload: AccessBatchRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        decisions = [
            await access_policy.authorize(payload.user_id, scene_id, clock())
            for scene_id in payload.scene_ids
        ]
        return {
            "items": [
                {
                    "scene_id": scene_id,
                    "level": decision.level,
                    "sources": decision.sources,
                    "earliest_expires_at": decision.earliest_expires_at,
                }
                for scene_id, decision in zip(payload.scene_ids, decisions, strict=True)
            ]
        }

    @router.post("/scenes/{scene_id}/open")
    async def open_scene(
        scene_id: str,
        payload: UserQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        now = clock()
        activation = (
            None
            if scene_activation is None
            else await scene_activation.activate_for_scene(payload.user_id, scene_id, now)
        )
        decision = await access_policy.authorize(payload.user_id, scene_id, now)
        if activation is not None and activation.activated_at is not None:
            decision = type(decision)(
                decision.level,
                decision.sources,
                decision.earliest_expires_at,
                activation.activated_at,
            )
        if decision.level is AccessLevel.HIDDEN:
            raise AppError("SCENE_ACCESS_DENIED", "无权访问该场景", 403)
        if decision.level is AccessLevel.PREVIEW:
            preview = await content_queries.get_preview_scene(scene_id)
            scene = None if preview is None else serialize_preview_scene(preview)
        else:
            scene = await content_queries.get_full_scene(scene_id)
        if scene is None:
            raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
        return {
            "access": decision.level,
            "sources": decision.sources,
            "earliest_expires_at": decision.earliest_expires_at,
            "activated_at": decision.activated_at,
            "scene": scene,
        }

    @router.post("/scenes/{scene_id}/entries/{entry_id}")
    async def get_entry(
        scene_id: str,
        entry_id: str,
        payload: UserQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        decision = await access_policy.authorize(payload.user_id, scene_id, clock())
        if not decision.has_full_access:
            raise AppError("SCENE_ACCESS_DENIED", "无权访问该条目", 403)
        entry = await content_queries.get_entry(scene_id, entry_id)
        if entry is None:
            raise AppError("ENTRY_NOT_FOUND", "条目不存在", 404)
        return entry

    return router
