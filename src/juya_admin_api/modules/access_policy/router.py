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
    async def entitlements(self, user_id: str, now: datetime) -> dict[str, object]:
        # 功能: 读取用户正式及限时权益概览.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 用户的正式与限时权益列表以及相关期限和学习成就.
        ...

    async def list_learning_modules(self) -> list[dict[str, object]]:
        # 功能: 查询可用于学习目录的已发布模块.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 发布的学习模块目录字段列表.
        ...

    async def learning_catalog(self, user_id: str) -> list[dict[str, object]]:
        # 功能: 生成指定用户可见的学习目录并合并授权状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 场景学习目录,带用户访问状态及权益来源.
        ...

    async def get_full_scene(self, scene_id: str) -> dict[str, object] | None:
        # 功能: 读取已发布场景的完整教学内容.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scene_id: 学习场景公开标识.
        # 返回: 已发布的完整场景快照;未找到时为 None.
        ...

    async def get_preview_scene(self, scene_id: str) -> dict[str, object] | None:
        # 功能: 读取场景允许公开的预览教学内容.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scene_id: 学习场景公开标识.
        # 返回: 场景预览快照;未找到时为 None.
        ...

    async def get_entry(self, scene_id: str, entry_id: str) -> dict[str, object] | None:
        # 功能: 读取指定场景中的词条并校验可见范围.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scene_id: 学习场景公开标识.
        #     entry_id: 场景内词条公开标识.
        # 返回: 匹配的场景词条快照,包含版本和来源.不存在时为 None.
        ...


class SceneActivationPort(Protocol):
    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> AccessDecision:
        # 功能: 首次访问活动场景时激活对应限时权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     scene_id: 学习场景公开标识.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 场景访问级别,授权来源及最早到期边界.
        ...


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
    # 功能: 生成预览场景响应快照.
    # 参数:
    #     scene: 场景内容的序列化快照.
    # 返回: 仅包含预览白名单字段的场景快照.
    return {key: scene[key] for key in PREVIEW_FIELDS if key in scene}


def entry_source_context(content: SceneContent, entry: SceneEntry, locator: str) -> str:
    # 功能: 根据来源定位符查找词条对应原句上下文.
    # 参数:
    #     content: 场景完整内容对象,包含原句和媒体关联.
    #     entry: 场景中需要定位原句上下文的词条.
    #     locator: 词条来源定位符,须匹配当前场景版本原句可点击片段或词表来源.
    # 返回: 定位原句的英文文本;词表来源返回词条英文,多个原句以换行拼接.
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


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
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
    # 功能: 创建小程序使用的内容目录,授权和资源签名内部路由.
    # 参数:
    #     access_policy: 场景访问判定服务,合并开放内容和用户权益.
    #     content_queries: 查询已发布场景,词条和学习目录的内部内容端口.
    #     current_service: 校验内部服务请求签名并注入服务身份的依赖.
    #     scene_activation: 首次打开场景时激活限时权益的业务端口.
    #     production_store: 查询内容制作草稿和媒体绑定的存储.
    #     media_service: 媒体对象查询,签名和安全校验服务.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/internal/v1", tags=["internal-content"])

    @router.get("/learning/modules")
    async def learning_modules(
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 返回小程序学习模块目录.
        # 参数:
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 封装学习模块目录的响应字典.
        return {"items": await content_queries.list_learning_modules()}

    @router.post("/learning/catalog")
    async def learning_catalog(
        payload: UserQuery,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 生成指定用户可见的学习目录并合并授权状态.
        # 参数:
        #     payload: 查询用户的公开标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 场景学习目录,带用户访问状态及权益来源.
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
        # 功能: 读取用户正式及限时权益概览.
        # 参数:
        #     payload: 查询用户的公开标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 用户的正式与限时权益列表以及相关期限和学习成就.
        return await content_queries.entitlements(payload.user_id, clock())

    @router.post("/access/batch")
    async def access_batch(
        payload: AccessBatchRequest,
        _principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能: 批量判定用户对多个场景的访问级别.
        # 参数:
        #     payload: 用户标识及待批量判断访问权限的场景集合.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 按输入场景返回访问级别,授权来源和最早到期时间的批量结果.
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
        # 功能: 打开场景并在授权允许时激活限时权益及返回教学内容.
        # 参数:
        #     scene_id: 学习场景公开标识.
        #     payload: 查询用户的公开标识.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 场景访问结果,授权来源,期限及完整或预览场景数据.
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
        # 功能: 读取指定场景中的词条并校验可见范围.
        # 参数:
        #     scene_id: 学习场景公开标识.
        #     entry_id: 场景内词条公开标识.
        #     payload: 访问词条的用户及来源定位信息.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 匹配的场景词条快照,包含版本和来源.
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
        # 功能: 校验场景及资源关联和访问权限后签发媒体地址.
        # 参数:
        #     scene_id: 学习场景公开标识.
        #     resource_id: 场景绑定的媒体资源公开标识.
        #     payload: 请求资源访问的用户及使用上下文.
        #     _principal: 内部签名校验后注入的调用服务身份.
        # 返回: 媒体资源标识,签名下载地址及过期时间.
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
