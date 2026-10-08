from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.domain import (
    AdminPreview,
    DiscoveryConfig,
    Scene,
    ScenePage,
    SceneRevision,
)
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.service import ContentService


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision_id: str | None = None


class PublishCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledged_warning_codes: set[str] = Field(default_factory=set)


class PublishRevisionRequest(PublishCheckRequest):
    expected_version: int = Field(ge=1)


class OpenScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_ids: list[str] = Field(min_length=3, max_length=3)


class PreviewScenesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene_ids: list[str] = Field(min_length=3, max_length=6)


class SaveRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    content: SceneContent


class SaveDiscoveryConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    open_scene_ids: list[str] = Field(min_length=3, max_length=3)
    preview_by_series: dict[str, list[str]]
    learning_modules: dict[str, bool]


class SceneResponse(BaseModel):
    template_type: str = "dialogue"
    id: str
    series_id: str
    title: str
    series_title: str
    summary: str | None
    cover_object_key: str | None
    status: str
    draft_revision_id: str | None
    published_revision_id: str | None
    updated_at: datetime | None


class ScenePageResponse(BaseModel):
    items: list[SceneResponse]
    page: int
    page_size: int
    total: int


class RevisionResponse(BaseModel):
    id: str
    scene_id: str
    source_revision_id: str | None
    version: int
    status: str
    stable_sentence_ids: list[str]
    stable_entry_ids: list[str]
    content: SceneContent
    created_by: str
    created_at: datetime | None


class RevisionHistoryItemResponse(BaseModel):
    id: str
    version_no: int
    edit_version: int
    status: str
    source_revision_id: str | None
    title_en: str | None
    created_at: datetime | None
    created_by: str
    is_current: bool


class RevisionHistoryResponse(BaseModel):
    items: list[RevisionHistoryItemResponse]
    page: int
    page_size: int
    total: int


class DiscoveryConfigResponse(BaseModel):
    version: int
    open_scene_ids: list[str]
    preview_by_series: dict[str, list[str]]
    learning_modules: dict[str, bool]
    updated_at: datetime | None
    actor_id: str | None


class AdminPreviewResponse(BaseModel):
    scene_id: str
    revision_id: str
    revision_status: str
    scene_title: str
    series_title: str
    content: SceneContent


AdminDependency = Callable[..., Awaitable[SessionRecord]]


def _serialize_scene(scene: Scene) -> dict[str, object]:
    # 功能:将场景领域对象转换为管理接口响应字段。
    # 参数:
    #     scene: 场景领域对象,含系列和当前草稿、发布版本引用。
    # 返回:场景详情响应字段。
    return {
        "id": scene.id,
        "series_id": scene.series_id,
        "template_type": scene.template_type,
        "title": scene.title,
        "series_title": scene.series_title,
        "summary": scene.summary,
        "cover_object_key": scene.cover_object_key,
        "status": scene.status,
        "draft_revision_id": scene.draft_revision_id,
        "published_revision_id": scene.published_revision_id,
        "updated_at": scene.updated_at,
    }


def _serialize_page(page: ScenePage) -> dict[str, object]:
    # 功能:将场景分页结果转换为列表和分页元信息。
    # 参数:
    #     page: 场景分页领域对象,含记录、页码和总数。
    # 返回:场景记录列表及分页元信息。
    return {
        "items": [_serialize_scene(item) for item in page.items],
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
    }


def _serialize_revision(revision: SceneRevision) -> dict[str, object]:
    # 功能:将内容版本及草稿快照转换为接口响应字段。
    # 参数:
    #     revision: 场景内容版本对象,含快照、编辑版本和状态。
    # 返回:内容版本详情及快照响应字段。
    return {
        "id": revision.id,
        "scene_id": revision.scene_id,
        "source_revision_id": revision.source_revision_id,
        "version": revision.version,
        "status": revision.status,
        "stable_sentence_ids": list(revision.stable_sentence_ids),
        "stable_entry_ids": list(revision.stable_entry_ids),
        "content": revision.content,
        "created_by": revision.created_by,
        "created_at": revision.created_at,
    }


def _serialize_config(config: DiscoveryConfig) -> dict[str, object]:
    # 功能:将发现页配置转换为接口响应字段。
    # 参数:
    #     config: 开放场景、系列预览和学习模块的发现页配置。
    # 返回:发现页配置和配置版本响应字段。
    return {
        "version": config.version,
        "open_scene_ids": list(config.open_scene_ids),
        "preview_by_series": {
            series_id: list(scene_ids) for series_id, scene_ids in config.preview_by_series.items()
        },
        "learning_modules": config.learning_modules,
        "updated_at": config.updated_at,
        "actor_id": config.actor_id,
    }


def _serialize_preview(preview: AdminPreview) -> dict[str, object]:
    # 功能:将管理端预览内容转换为接口响应字段。
    # 参数:
    #     preview: 管理端预览对象,包含内容版本和完整场景快照。
    # 返回:管理端场景预览响应字段。
    return {
        "scene_id": preview.scene_id,
        "revision_id": preview.revision_id,
        "revision_status": preview.revision_status,
        "scene_title": preview.scene_title,
        "series_title": preview.series_title,
        "content": preview.content,
    }


# 匿名函数: 提供可注入时钟的当前 UTC 时间。
# 参数: 无。
# 返回: 带 UTC 时区的当前时间。
def create_content_router(
    service: ContentService,
    *,
    audit_service: AuditService,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能:注册内容列表、草稿编辑、发布和发现页配置接口。
    # 参数:
    #     service: 内容服务,读取和保存场景草稿、执行发布及配置校验。
    #     audit_service: 审计服务,保存操作人、请求标识和业务变更摘要。
    #     current_admin: 管理员读取权限依赖,验证会话并返回管理员身份。
    #     current_admin_write: 管理员写入权限依赖,验证会话及写操作权限。
    #     clock: 返回当前时间的可注入时钟,供审计、签名和作业状态更新。
    # 返回:已注册对应业务接口和权限依赖的 FastAPI 路由器。
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    @router.get("/scenes", response_model=ScenePageResponse)
    async def list_scenes(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        query: Annotated[str | None, Query(max_length=200)] = None,
        series_id: str | None = None,
        status: Literal["DRAFT", "PUBLISHED", "OFFLINE"] | None = None,
    ) -> dict[str, object]:
        # 功能:按系列、状态和检索条件分页读取场景。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     status: 待写入或筛选的业务状态代码。
        # 返回:场景列表及页码、每页条数和总数。
        result = await service.list_scenes(
            page=page,
            page_size=page_size,
            query=query,
            series_id=series_id,
            status=status,
        )
        return _serialize_page(result)

    @router.get("/scenes/{scene_id}", response_model=SceneResponse)
    async def get_scene(
        scene_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:按公开标识读取场景及其当前版本引用。
        # 参数:
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:场景详情接口字段及其当前版本引用。
        return _serialize_scene(await service.get_scene(scene_id))

    @router.get("/revisions/{revision_id}", response_model=RevisionResponse)
    async def get_revision(
        revision_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:按公开标识读取内容版本和编辑状态。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:内容版本标识、编辑状态和完整草稿快照。
        return _serialize_revision(await service.get_revision(revision_id))

    @router.put("/revisions/{revision_id}", response_model=RevisionResponse)
    async def save_revision(
        revision_id: str,
        payload: SaveRevisionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     payload: 草稿保存请求,包含预期编辑版本和新内容快照。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:内容版本标识、编辑状态和完整草稿快照。
        previous = await service.get_revision(revision_id)
        saved = await service.save_revision(
            revision_id,
            payload.content.model_dump(mode="json"),
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
        )
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action="content.revision.save",
                object_type="scene_revision",
                object_public_id=revision_id,
                before_summary={"version": previous.version, "status": previous.status},
                after_summary={"version": saved.version, "status": saved.status},
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )
        return _serialize_revision(saved)

    @router.get("/discovery-config", response_model=DiscoveryConfigResponse)
    async def get_discovery_config(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:读取开放场景、系列预览和学习模块配置。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:发现页配置内容和配置版本字段。
        return _serialize_config(await service.get_discovery_config())

    @router.put("/discovery-config", response_model=DiscoveryConfigResponse)
    async def save_discovery_config(
        payload: SaveDiscoveryConfigRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:按预期配置版本保存开放场景、系列预览和学习模块开关。
        # 参数:
        #     payload: 发现页配置请求,包含预期版本、开放场景、系列预览和模块开关。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:发现页配置内容和配置版本字段。
        open_scene_ids = cast(tuple[str, str, str], tuple(payload.open_scene_ids))
        saved = await service.save_discovery_config(
            open_scene_ids=open_scene_ids,
            preview_by_series={
                series_id: tuple(scene_ids)
                for series_id, scene_ids in payload.preview_by_series.items()
            },
            learning_modules=dict(payload.learning_modules),
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action="content.discovery-config.save",
                object_type="discovery_config",
                object_public_id="global",
                before_summary={"version": payload.expected_version},
                after_summary={"version": saved.version},
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )
        return _serialize_config(saved)

    @router.get("/revisions/{revision_id}/preview", response_model=AdminPreviewResponse)
    async def admin_preview(
        revision_id: str,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:读取内容版本的管理端预览快照。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     response: 当前 HTTP 响应对象,设置禁止缓存的响应头。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:管理端预览的场景、版本和内容快照字段。
        response.headers["Cache-Control"] = "no-store"
        return _serialize_preview(await service.admin_preview(revision_id))

    @router.get("/scenes/{scene_id}/revisions", response_model=RevisionHistoryResponse)
    async def revision_history(
        scene_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        # 功能:读取场景版本历史并返回分页接口响应。
        # 参数:
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:内容版本历史的分页响应字段。
        return await service.list_revision_history(scene_id, page=page, page_size=page_size)

    @router.post("/scenes/{scene_id}/revisions", status_code=201)
    async def create_revision(
        scene_id: str,
        payload: CreateRevisionRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:从指定来源版本复制内容,创建新的场景草稿。
        # 参数:
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     payload: 新草稿请求,指定可选的来源内容版本。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:内容版本标识、编辑状态和完整草稿快照。
        revision = await service.create_revision(
            scene_id,
            payload.source_revision_id,
            str(admin.admin_user_id),
            clock(),
        )
        return _serialize_revision(revision)

    @router.post("/revisions/{revision_id}/publish-checks")
    async def validate_publish(
        revision_id: str,
        payload: PublishCheckRequest,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:校验发布错误和提醒确认情况,未满足条件时阻止发布。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     payload: 发布预检查请求,包含已确认的提醒代码。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:发布就绪状态、错误和提醒代码。
        revision = await service.get_revision(revision_id)
        summary = await service.inspect_publish(
            revision_id, frozenset(payload.acknowledged_warning_codes)
        )
        return {
            "revision_id": summary.revision_id,
            "version": revision.version,
            "ready": summary.ready,
            "error_codes": summary.error_codes,
            "warning_codes": summary.warning_codes,
        }

    @router.post("/revisions/{revision_id}/commands/publish")
    async def publish_revision(
        revision_id: str,
        payload: PublishRevisionRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能:核对草稿编辑版本和发布条件后发布内容。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     payload: 内容发布请求,包含预期编辑版本和已确认提醒。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:已发布场景、内容版本和发布时间字段。
        published = await service.publish_revision(
            revision_id,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
            acknowledged_warning_codes=frozenset(payload.acknowledged_warning_codes),
            expected_version=payload.expected_version,
        )
        return {
            "scene_id": published.scene_id,
            "revision_id": published.revision_id,
            "published_at": published.published_at,
        }

    @router.put("/open-scenes")
    async def replace_open_scenes(
        payload: OpenScenesRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:校验三个不同的已发布场景并替换开放配置。
        # 参数:
        #     payload: 开放场景配置请求,指定三个不同的场景标识。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:保存后的场景配置及生效时间字段。
        scene_ids = cast(tuple[str, str, str], tuple(payload.scene_ids))
        config = await service.replace_open_scenes(scene_ids, str(admin.admin_user_id), clock())
        return {
            "version": config.version,
            "scene_ids": config.scene_ids,
            "activated_at": config.activated_at,
        }

    @router.put("/preview-configs/{series_id}")
    async def replace_preview_scenes(
        series_id: str,
        payload: PreviewScenesRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:校验本系列三至六个已发布场景并替换预览配置。
        # 参数:
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     payload: 系列预览配置请求,指定三至六个不同场景标识。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:保存后的场景配置及生效时间字段。
        config = await service.replace_preview_scenes(
            series_id,
            tuple(payload.scene_ids),
            str(admin.admin_user_id),
            clock(),
        )
        return {
            "series_id": config.series_id,
            "scene_ids": config.scene_ids,
            "updated_at": config.updated_at,
        }

    @router.post("/scenes/{scene_id}/commands/offline", status_code=204)
    async def offline_scene(
        scene_id: str,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> None:
        # 功能:校验场景未被开放或预览配置引用后将其下线。
        # 参数:
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        await service.offline_scene(scene_id, str(admin.admin_user_id), clock())

    return router
