from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.access_policy.service import AccessPolicyService
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.media.service import MediaService
from juya_admin_api.shared.errors import AppError


class InternalContentQueryPort(Protocol):
    async def entitlements(self, user_id: str, now: datetime) -> dict[str, object]: ...
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


class ResourceQuery(UserQuery):
    revision_id: str = Field(min_length=1, max_length=64)


class EntryQuery(ResourceQuery):
    entry_version: int = Field(ge=1)
    source_locator: str = Field(min_length=1, max_length=255)


class PublishedSceneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_id: str
    revision_id: str
    content_version: int = Field(ge=1)
    content: SceneContent


class PreviewSceneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_id: str
    title: str
    title_en: str = ""
    title_zh: str = ""
    series: str | None = None
    cover_url: str | None = None
    introduction: str | None = None
    preview_status: Literal["PREVIEW"] = "PREVIEW"


class SceneOpenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access: Literal["OPEN", "FORMAL", "LIMITED", "PREVIEW"]
    sources: list[str]
    earliest_expires_at: datetime | None = None
    activated_at: datetime | None = None
    scene: PublishedSceneResponse | PreviewSceneResponse


class AuthorizedEntryResponse(SceneEntry):
    scene_id: str
    revision_id: str
    source_locator: str
    sentence_snapshot: str
    entry_type: Literal["VOCABULARY", "PHRASE"]


class SignedSceneResourceResponse(BaseModel):
    resource_id: str
    url: str
    expires_at: datetime


ServiceDependency = Callable[..., Awaitable[ServicePrincipal]]
PREVIEW_FIELDS = frozenset(
    {
        "public_id",
        "title",
        "title_en",
        "title_zh",
        "series",
        "cover_url",
        "introduction",
        "preview_status",
    }
)


def serialize_preview_scene(scene: dict[str, object]) -> dict[str, object]:
    return {key: scene[key] for key in PREVIEW_FIELDS if key in scene}


def entry_source_context(content: SceneContent, entry: SceneEntry, locator: str) -> str:
    sentences = [
        sentence
        for sentence in content.dialogue
        if any(
            span.source_locator == locator
            and span.entry_id == entry.entry_id
            and span.entry_version == entry.entry_version
            for span in sentence.clickable_spans
        )
    ]
    list_source = f"{'vocabulary' if entry in content.vocabulary else 'chunks'}:{entry.entry_id}"
    if not sentences and locator != list_source:
        raise AppError("ENTRY_SOURCE_INVALID", "词条来源不属于当前场景版本", 403)
    return "\n".join(sentence.english for sentence in sentences) if sentences else entry.english


def create_internal_content_router(
    access_policy: AccessPolicyService,
    content_queries: InternalContentQueryPort,
    *,
    current_service: ServiceDependency,
    scene_activation: SceneActivationPort | None = None,
    production_store: ProductionStore | None = None,
    media_service: MediaService | None = None,
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
        items: list[dict[str, object]] = []
        now = clock()
        for row in await content_queries.learning_catalog(payload.user_id):
            scene_id = str(row.get("scene_id") or row.get("public_id") or "")
            decision = await access_policy.authorize(payload.user_id, scene_id, now)
            if decision.level == AccessLevel.HIDDEN:
                continue
            image_url = row.get("image_url")
            if media_service is not None and row.get("cover_object_key"):
                try:
                    cover = await media_service.sign_media(str(row["cover_object_key"]), None, now)
                    image_url = cover.url
                except AppError:
                    image_url = None
            items.append(
                {
                    "public_id": scene_id,
                    "scene_id": scene_id,
                    "access": decision.level,
                    "title": row.get("title_en") or row.get("title") or "",
                    "chinese_title": row.get("title_zh") or row.get("title") or "",
                    "series": row.get("series") or "",
                    "tags": row.get("tags") or [],
                    "description": row.get("summary") or "",
                    "image_url": image_url,
                    "earliest_expires_at": decision.earliest_expires_at,
                    **(
                        {"trial_sentence": row["trial_sentence"]}
                        if decision.level == AccessLevel.OPEN and row.get("trial_sentence")
                        else {}
                    ),
                }
            )
        return {"items": items}

    @router.post("/entitlements")
    async def entitlements(
        payload: UserQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        return await content_queries.entitlements(payload.user_id, clock())

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

    @router.post("/scenes/{scene_id}/open", response_model=SceneOpenResponse)
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
            if (
                preview is not None
                and preview.get("cover_object_key")
                and media_service is not None
            ):
                cover = await media_service.sign_media(str(preview["cover_object_key"]), None, now)
                preview["cover_url"] = cover.url
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

    @router.post("/scenes/{scene_id}/entries/{entry_id}", response_model=AuthorizedEntryResponse)
    async def get_entry(
        scene_id: str,
        entry_id: str,
        payload: EntryQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        decision = await access_policy.authorize(payload.user_id, scene_id, clock())
        if not decision.has_full_access:
            raise AppError("SCENE_ACCESS_DENIED", "无权访问该条目", 403)
        snapshot = await content_queries.get_full_scene(scene_id)
        if snapshot is None or snapshot.get("revision_id") != payload.revision_id:
            raise AppError("SCENE_VERSION_CONFLICT", "场景版本已变化; 请重新加载", 409)
        content = SceneContent.model_validate(snapshot["content"])
        entry = next(
            (
                entry
                for entry in [*content.vocabulary, *content.chunks]
                if entry.entry_id == entry_id and entry.entry_version == payload.entry_version
            ),
            None,
        )
        if entry is None:
            raise AppError("ENTRY_NOT_FOUND", "条目不存在", 404)
        context = entry_source_context(content, entry, payload.source_locator)
        return {
            **entry.model_dump(mode="json"),
            "scene_id": scene_id,
            "revision_id": payload.revision_id,
            "source_locator": payload.source_locator,
            "sentence_snapshot": context,
            "entry_type": "VOCABULARY" if entry in content.vocabulary else "PHRASE",
        }

    @router.post(
        "/scenes/{scene_id}/resources/{resource_id}/signed-url",
        response_model=SignedSceneResourceResponse,
    )
    async def signed_resource(
        scene_id: str,
        resource_id: str,
        payload: ResourceQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        decision = await access_policy.authorize(payload.user_id, scene_id, clock())
        if not decision.has_full_access:
            raise AppError("SCENE_ACCESS_DENIED", "无权访问场景资源", 403)
        if production_store is None or media_service is None:
            raise AppError("RESOURCE_UNAVAILABLE", "资源服务未配置", 503)
        fact = await production_store.resource(
            scene_id, payload.revision_id, resource_id, published=True
        )
        signed = await media_service.sign_media(
            str(fact["object_key"]), decision.earliest_expires_at, clock()
        )
        return {"resource_id": resource_id, "url": signed.url, "expires_at": signed.expires_at}

    return router
