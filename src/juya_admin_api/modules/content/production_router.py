"""Authoring endpoints sharing one structured draft and fixed resource references."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.router import (
    AdminDependency,
    RevisionResponse,
    SceneResponse,
    _serialize_revision,
    _serialize_scene,
)
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.service import MediaAdminService, MediaService
from juya_admin_api.modules.ocr_suggestions.service import OcrSuggestions, suggest_groups
from juya_admin_api.shared.errors import AppError


class CreateSeriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    cover_asset_id: str | None = None


class CreateSceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_id: str
    template_type: Literal["dialogue", "vocabulary"] = "dialogue"


class ImportImagesRequest(CreateSceneRequest):
    asset_ids: list[str] = Field(min_length=1, max_length=30)


class ImportImagesResponse(BaseModel):
    items: list[SceneResponse]
    reused_scene_ids: list[str]


class LexiconWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_type: Literal["VOCABULARY", "PHRASE"]
    entry: SceneEntry


class OcrAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    expected_version: int = Field(ge=1)
    selected_fields: list[Literal["title_en", "title_zh", "dialogue", "vocabulary", "chunks"]] = (
        Field(min_length=1, max_length=5)
    )
    content: SceneContent


def create_production_content_router(
    store: ProductionStore,
    content: ContentService,
    media: MediaService,
    media_admin: MediaAdminService,
    *,
    audit_service: AuditService,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    # 功能:注册系列、草稿、词典和 OCR 采纳的内容生产接口。
    # 参数:
    #     store: 内容生产事务存储,管理系列、词典版本和内容引用。
    #     content: 内容服务,读取和保存场景草稿并执行发布前校验。
    #     media: 素材服务,提供素材校验、不可变对象准备和访问签名。
    #     media_admin: 媒体管理服务,读取和确认 OCR、音频和批任务结果。
    #     audit_service: 审计服务,保存操作人、请求标识和业务变更摘要。
    #     current_admin: 管理员读取权限依赖,验证会话并返回管理员身份。
    #     current_admin_write: 管理员写入权限依赖,验证会话及写操作权限。
    # 返回:已注册对应业务接口和权限依赖的 FastAPI 路由器。
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    async def audit(
        admin: SessionRecord,
        action: str,
        object_id: str,
        request: Request,
        summary: dict[str, object],
    ) -> None:
        # 功能:记录操作人、业务对象和变更结果的审计事件。
        # 参数:
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     action: 业务命令或审计动作代码,标识本次状态迁移或变更类型。
        #     object_id: 审计事件所对应业务对象的公开标识。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     summary: 业务变更摘要,作为审计事件的操作后信息。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        await audit_service.record(
            AuditEvent(
                str(admin.admin_user_id),
                action,
                "CONTENT",
                object_id,
                {},
                summary,
                None,
                get_request_id(request),
                datetime.now(UTC),
            )
        )

    @router.post("/imports", response_model=ImportImagesResponse, status_code=201)
    async def import_images(
        payload: ImportImagesRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, object]:
        # 功能:校验已确认原图并按系列和模板创建或复用场景草稿。
        # 参数:
        #     payload: 原图导入请求,指定系列、模板和一至三十个图片素材标识。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:导入场景详情及复用场景编号,帮助管理员确认是否新增记录
        result = await store.import_images(
            payload.series_id,
            payload.template_type,
            payload.asset_ids,
            str(admin.admin_user_id),
            idempotency_key,
        )
        await audit(
            admin,
            "CONTENT_IMAGES_IMPORTED",
            payload.series_id,
            request,
            {"count": len(result["scene_ids"]), "reused_count": len(result["reused_scene_ids"])},
        )
        return {
            "items": [
                _serialize_scene(await content.get_scene(scene_id))
                for scene_id in result["scene_ids"]
            ],
            "reused_scene_ids": result["reused_scene_ids"],
        }

    @router.get("/series")
    async def list_series(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        # 功能:读取内容系列及展示排序信息。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:包含 items 记录列表的接口响应。
        return {"items": await store.list_series()}

    @router.post("/series", status_code=201)
    async def create_series(
        payload: CreateSeriesRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, Any]:
        # 功能:按幂等创建请求新增内容系列和封面引用。
        # 参数:
        #     payload: 系列创建请求,包含展示标题、短标识和可选封面素材。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:已创建或由幂等记录重放的系列字段。
        result = await store.create_series(
            payload.title.strip(),
            payload.slug,
            payload.cover_asset_id,
            actor=str(admin.admin_user_id),
            key=idempotency_key,
        )
        await audit(
            admin, "CONTENT_SERIES_CREATED", result["id"], request, {"title": payload.title}
        )
        return result

    @router.post("/scenes", response_model=SceneResponse, status_code=201)
    async def create_scene(
        payload: CreateSceneRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[
            str, Header(alias="X-Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> dict[str, object]:
        # 功能:为内容系列创建指定模板的空场景和初始草稿。
        # 参数:
        #     payload: 场景创建请求,指定所属系列和内容模板类型。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:场景详情接口字段及其当前版本引用。
        scene_id = await store.create_scene(
            payload.series_id,
            payload.template_type,
            actor=str(admin.admin_user_id),
            key=idempotency_key,
        )
        await audit(
            admin, "CONTENT_SCENE_CREATED", scene_id, request, {"series_id": payload.series_id}
        )
        return _serialize_scene(await content.get_scene(scene_id))

    @router.get("/lexicon")
    async def list_lexicon(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        query: Annotated[str, Query(max_length=500)] = "",
    ) -> dict[str, Any]:
        # 功能:按英文检索条件读取词汇和语块词典。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        # 返回:包含 items 记录列表的接口响应。
        return {"items": await store.list_lexicon(query)}

    @router.post("/lexicon", response_model=SceneEntry, status_code=201)
    async def create_lexicon(
        payload: LexiconWriteRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> SceneEntry:
        # 功能:保存词汇或语块并生成可引用的词典版本。
        # 参数:
        #     payload: 词典写入请求,包含词条类别和完整词条内容。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:已保存的词典条目及其版本引用。
        result = await store.write_entry(
            payload.entry, payload.entry_type, str(admin.admin_user_id)
        )
        await audit(
            admin,
            "LEXICON_SAVED",
            result.entry_id,
            request,
            {"entry_version": result.entry_version},
        )
        return result

    @router.put("/lexicon/{entry_id}", response_model=SceneEntry)
    async def update_lexicon(
        entry_id: str,
        payload: LexiconWriteRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> SceneEntry:
        # 功能:校验路径词条标识后保存词典的新版本。
        # 参数:
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     payload: 词典写入请求,包含词条类别和完整词条内容。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:已保存的词典条目及其版本引用。
        if payload.entry.entry_id != entry_id:
            raise AppError("ENTRY_REFERENCE_INVALID", "词条引用不匹配", 422)
        return await create_lexicon(payload, request, admin)

    @router.get("/revisions/{revision_id}/ocr-suggestions/{job_id}", response_model=OcrSuggestions)
    async def ocr_suggestions(
        revision_id: str,
        job_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> OcrSuggestions:
        # 功能:核对 OCR 候选与草稿原图关系后生成内容分组建议。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:OCR 行、四组采纳建议和未分配行标识。
        revision = await content.get_revision(revision_id)
        job = await media_admin.get_job(job_id)
        candidate = await media_admin.get_ocr_candidate(job_id)
        if (
            job.input_payload.get("revision_id") != revision_id
            or job.input_payload.get("scene_id") != revision.scene_id
            or candidate.asset_id != revision.content.get("original_image_asset_id")
        ):
            raise AppError("OCR_REVISION_INVALID", "OCR 候选不属于当前草稿及原图", 409)
        scene = await content.get_scene(revision.scene_id)
        return suggest_groups(candidate.structured_candidate, scene.template_type)

    @router.post("/revisions/{revision_id}/ocr-adoptions", response_model=RevisionResponse)
    async def adopt_ocr(
        revision_id: str,
        payload: OcrAdoptionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:采纳指定 OCR 字段到草稿并确认候选和记录审计。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     payload: OCR 采纳请求,包含作业、预期草稿版本、选中字段和校对内容。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:内容版本标识、编辑状态和完整草稿快照。
        candidate = await media_admin.get_ocr_candidate(payload.job_id)
        job = await media_admin.get_job(payload.job_id)
        revision = await content.get_revision(revision_id)
        if (
            job.input_payload.get("revision_id") != revision_id
            or job.input_payload.get("scene_id") != revision.scene_id
            or candidate.asset_id != revision.content.get("original_image_asset_id")
        ):
            raise AppError("OCR_REVISION_INVALID", "OCR 候选不属于当前草稿及原图", 409)
        if candidate.confirmed_revision_id is not None:
            raise AppError("OCR_ALREADY_ADOPTED", "OCR 候选已采纳", 409)
        merged = SceneContent.model_validate(revision.content).model_dump(mode="json")
        proposed = payload.content.model_dump(mode="json")
        for field in set(payload.selected_fields):
            merged[field] = proposed[field]
        saved = await content.save_revision(
            revision_id,
            merged,
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
        )
        await media_admin.confirm_ocr_candidate(
            payload.job_id, revision_id, actor_id=str(admin.admin_user_id), now=datetime.now(UTC)
        )
        await audit(
            admin,
            "OCR_ADOPTED",
            revision_id,
            request,
            {
                "job_id": payload.job_id,
                "version": saved.version,
                "selected_fields": payload.selected_fields,
            },
        )
        return _serialize_revision(saved)

    @router.get("/revisions/{revision_id}/resources/{resource_id}/signed-url")
    async def draft_resource(
        revision_id: str,
        resource_id: str,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能:校验资源属于草稿后生成禁止缓存的资源访问签名。
        # 参数:
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     resource_id: 内容快照内的素材、音频目标或音频版本标识。
        #     response: 当前 HTTP 响应对象,设置禁止缓存的响应头。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:草稿资源标识、访问 URL 和其到期时间。
        revision = await content.get_revision(revision_id)
        fact = await store.resource(revision.scene_id, revision_id, resource_id, published=False)
        signed = await media.sign_media(str(fact["object_key"]), None, datetime.now(UTC))
        response.headers["Cache-Control"] = "no-store"
        return {"resource_id": resource_id, "url": signed.url, "expires_at": signed.expires_at}

    return router
