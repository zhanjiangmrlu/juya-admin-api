from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.infrastructure.observability.request_id import get_request_id
from juya_admin_api.integrations.ocr.baidu import validate_ocr_image
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.batch_executor import BATCH_OPERATIONS
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.domain import (
    AudioTarget,
    AudioVersion,
    BatchJob,
    BatchJobItem,
    OcrCandidate,
    ProcessingJob,
    TrashEntry,
)
from juya_admin_api.modules.media.quota import OcrQuotaService
from juya_admin_api.modules.media.service import MediaAdminService, MediaAsset, MediaService
from juya_admin_api.shared.errors import AppError


class UploadPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str


class ConfirmUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: str
    object_key: str = Field(min_length=1, max_length=512)


class CreateOcrJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64)
    object_key: str | None = Field(default=None, min_length=1, max_length=512)
    scene_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    series_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)


class OcrCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str | None = Field(default=None, max_length=64)
    content: dict[str, object] | None = None


class CreateAudioTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stable_key: str = Field(min_length=1, max_length=128)
    target_type: str = Field(min_length=1, max_length=32)


class OcrSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    monthly_limit: int = Field(ge=0, le=1_000_000)
    free_quota: int = Field(ge=0, le=1_000_000)
    paid_disabled: bool
    verify_quota: bool = False


class CreateAudioVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1, max_length=64)


class GenerateAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_key: str = Field(min_length=1, max_length=128)
    target_type: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=5000)
    voice: str = Field(min_length=1, max_length=64)


class RollbackAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)


class CreateBatchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: str = Field(min_length=1, max_length=32)
    target_ids: list[str] = Field(min_length=1, max_length=500)
    input_payload: dict[str, object] = Field(default_factory=dict)


class CreateTrashRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)


class EmptyCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessingJobResponse(BaseModel):
    id: str
    business_key: str
    job_type: str
    target_id: str
    batch_id: str | None
    status: str
    provider_request_id: str | None
    error_code: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None


class OcrCandidateResponse(BaseModel):
    id: str
    job_id: str
    asset_id: str
    status: str
    template_type: str
    structured_candidate: dict[str, object]
    confidence: float | None
    error_code: str | None
    confirmed_revision_id: str | None


class OcrConfirmationResponse(BaseModel):
    revision_id: str
    revision_status: str
    version: int


class AudioTargetResponse(BaseModel):
    id: str
    stable_key: str
    target_type: str
    active_version_id: str | None


class AudioTargetListResponse(BaseModel):
    items: list[AudioTargetResponse]


class AudioVersionResponse(BaseModel):
    id: str
    target_id: str
    asset_id: str
    version_no: int
    source: str
    status: str
    provider_request_id: str | None
    processing_job_id: str | None
    created_by: str
    created_at: datetime


class AudioVersionListResponse(BaseModel):
    items: list[AudioVersionResponse]


class BatchJobItemResponse(BaseModel):
    id: str
    item_key: str
    target_id: str
    status: str
    attempt_count: int
    error_code: str | None
    result_version: int | None
    processing_job_id: str | None


class BatchJobResponse(BaseModel):
    id: str
    business_key: str
    job_type: str
    status: str
    total_count: int
    success_count: int
    failure_count: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    cancel_requested_at: datetime | None
    items: list[BatchJobItemResponse]
    input_payload: dict[str, object]
    result_payload: dict[str, object]


class BatchJobPageResponse(BaseModel):
    items: list[BatchJobResponse]
    page: int
    page_size: int
    total: int


class TrashEntryResponse(BaseModel):
    id: str
    scene_id: str
    revision_id: str
    status: str
    trashed_by: str
    trashed_at: datetime
    retention_until: datetime
    restored_at: datetime | None
    cleaned_at: datetime | None


class TrashListResponse(BaseModel):
    items: list[TrashEntryResponse]


AdminDependency = Callable[..., Awaitable[SessionRecord]]
SignedTargetResolver = Callable[[str, str, datetime], Awaitable[tuple[str, datetime | None]]]
IdempotencyKey = Annotated[
    str,
    Header(alias="X-Idempotency-Key", min_length=1, max_length=128),
]


class MediaTaskDispatcher(Protocol):
    async def enqueue_batch(self, batch_id: str) -> None:
        # 功能:检查调度开关后将内容批任务发送到工作队列。
        # 参数:
        #     self: 当前 MediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def enqueue_ocr(self, job_id: str, object_key: str, template_type: str) -> None:
        # 功能:将指定原图的 OCR 作业发送到工作队列。
        # 参数:
        #     self: 当前 MediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     template_type: 内容模板类型,区分 dialogue 对话与 vocabulary 词汇。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None:
        # 功能:将指定音频目标的语音生成作业发送到工作队列。
        # 参数:
        #     self: 当前 MediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;
        #         兼容大写输入。
        #     text: 需要生成语音的原始文本。
        #     voice: 语音合成声线标识,传递给合成供应商。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...


# 匿名函数: 提供可注入时钟的当前 UTC 时间。
# 参数: 无。
# 返回: 带 UTC 时区的当前时间。
def create_media_router(
    service: MediaService,
    *,
    current_admin_write: AdminDependency,
    admin_service: MediaAdminService | None = None,
    content_service: ContentService | None = None,
    audit_service: AuditService | None = None,
    task_dispatcher: MediaTaskDispatcher | None = None,
    ocr_quota_service: OcrQuotaService | None = None,
    current_admin: AdminDependency | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能:注册素材上传、OCR、音频、批任务和回收站管理接口。
    # 参数:
    #     service: 素材服务,生成上传凭据、确认素材并签名资源访问。
    #     current_admin_write: 管理员写入权限依赖,验证会话及写操作权限。
    #     admin_service: 媒体管理服务,创建和更新作业、音频版本及批任务。
    #     content_service: 内容管理服务,读取场景草稿并更新内容快照。
    #     audit_service: 审计服务,保存操作人、请求标识和业务变更摘要。
    #     task_dispatcher: 媒体后台任务调度器,向队列发送 OCR、语音和批任务。
    #     ocr_quota_service: OCR 额度服务,控制每月预占次数和付费关闭策略。
    #     current_admin: 管理员读取权限依赖,验证会话并返回管理员身份。
    #     clock: 返回当前时间的可注入时钟,供审计、签名和作业状态更新。
    # 返回:已注册对应业务接口和权限依赖的 FastAPI 路由器。
    router = APIRouter(prefix="/api/v1/admin/media", tags=["admin-media"])
    read_admin = current_admin or current_admin_write

    def media_admin() -> MediaAdminService:
        # 功能:读取已配置的媒体管理服务,缺失时拒绝请求。
        # 参数:无。
        # 返回:已配置的媒体管理服务。
        if admin_service is None:
            raise AppError("MEDIA_ADMIN_UNAVAILABLE", "媒体任务管理能力不可用", 503)
        return admin_service

    def content_admin() -> ContentService:
        # 功能:读取已配置的内容管理服务,缺失时拒绝请求。
        # 参数:无。
        # 返回:已配置的内容管理服务。
        if content_service is None:
            raise AppError("CONTENT_ADMIN_UNAVAILABLE", "内容管理能力不可用", 503)
        return content_service

    def dispatcher() -> MediaTaskDispatcher:
        # 功能:读取已配置的媒体任务调度器,缺失时拒绝请求。
        # 参数:无。
        # 返回:已配置的后台任务调度器。
        if task_dispatcher is None:
            raise AppError("MEDIA_TASKS_UNAVAILABLE", "媒体任务调度能力不可用", 503)
        return task_dispatcher

    async def audit(
        request: Request,
        admin: SessionRecord,
        action: str,
        object_type: str,
        object_id: str,
        after: dict[str, object],
    ) -> None:
        # 功能:记录操作人、业务对象和变更结果的审计事件。
        # 参数:
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     action: 业务命令或审计动作代码,标识本次状态迁移或变更类型。
        #     object_type: 审计对象类型代码,区分内容、作业、音频或回收站记录。
        #     object_id: 审计事件所对应业务对象的公开标识。
        #     after: 操作后的业务字段摘要,供审计记录追踪变更。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        if audit_service is None:
            return
        await audit_service.record(
            AuditEvent(
                actor_public_id=str(admin.admin_user_id),
                action=action,
                object_type=object_type,
                object_public_id=object_id,
                before_summary={},
                after_summary=after,
                reason=None,
                request_id=get_request_id(request),
                occurred_at=clock(),
            )
        )

    @router.post("/upload-policies")
    async def create_upload_policy(
        payload: UploadPolicyRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:按素材类型和上传人生成受大小限制的上传凭据。
        # 参数:
        #     payload: 素材上传策略请求,指定图片或音频素材类型。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:上传地址、对象键前缀、最大字节数、有效秒数和表单凭据。
        policy = await service.create_upload_policy(payload.asset_type, str(admin.admin_user_id))
        return {
            "upload_url": policy.upload_url,
            "object_key_prefix": policy.object_key_prefix,
            "max_bytes": policy.max_bytes,
            "expires_in": policy.expires_in,
            "fields": policy.fields,
        }

    @router.post("/uploads/confirm", status_code=201)
    async def confirm_upload(
        payload: ConfirmUploadRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        # 功能:校验上传归属,解码并冻结素材,去重后保存审核状态。
        # 参数:
        #     payload: 上传确认请求,指定素材类型和已上传的对象存储键。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        # 返回:素材登记信息、审核状态和实际尺寸或时长。
        asset = await service.confirm_upload(
            payload.asset_type, str(admin.admin_user_id), payload.object_key, clock()
        )
        return _asset_response(asset)

    @router.get("/assets/{asset_id}")
    async def get_asset(
        asset_id: str, _admin: Annotated[SessionRecord, Depends(read_admin)]
    ) -> dict[str, object]:
        # 功能:读取素材并校验可用状态和不可变对象引用。
        # 参数:
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:素材登记信息、审核状态和实际尺寸或时长。
        return _asset_response(await service.get_asset(asset_id))

    @router.get("/assets/{asset_id}/signed-url")
    async def preview_asset(
        asset_id: str, _admin: Annotated[SessionRecord, Depends(read_admin)]
    ) -> dict[str, object]:
        # 功能:读取已确认素材并生成管理端预览签名。
        # 参数:
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:预览资源访问 URL 和其到期时间。
        asset = await service.get_asset(asset_id)
        signed = await service.sign_media(asset.object_key, None, clock())
        return {"url": signed.url, "expires_at": signed.expires_at}

    def quota() -> OcrQuotaService:
        # 功能:读取已配置的 OCR 额度服务,缺失时拒绝请求。
        # 参数:无。
        # 返回:已配置的 OCR 额度服务。
        if ocr_quota_service is None:
            raise AppError("OCR_DISABLED", "OCR额度管理未配置", 503)
        return ocr_quota_service

    @router.get("/ocr/quota")
    async def get_quota(_admin: Annotated[SessionRecord, Depends(read_admin)]) -> dict[str, object]:
        # 功能:返回 OCR 当月配置、使用量和剩余额度。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:OCR 配置、当前月份和调用额度使用状态。
        return await quota().status(clock())

    @router.put("/ocr/settings")
    async def configure_ocr(
        payload: OcrSettingsRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _key: IdempotencyKey,
    ) -> dict[str, object]:
        # 功能:更新 OCR 额度和付费调用策略并记录审计。
        # 参数:
        #     payload: OCR 配置请求,包含启用开关、免费和内部额度及付费关闭核实状态。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     _key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:配置更新后的 OCR 额度状态。
        result = await quota().configure(
            **payload.model_dump(), actor_id=str(admin.admin_user_id), now=clock()
        )
        await audit(
            request, admin, "media.ocr.settings", "ocr_settings", "1", jsonable_encoder(result)
        )
        return result

    @router.post("/ocr/jobs", status_code=201, response_model=ProcessingJobResponse)
    async def create_ocr_job(
        payload: CreateOcrJobRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse:
        # 功能:核对当前草稿和上传原图,创建 OCR 作业并预占额度后调度。
        # 参数:
        #     payload: OCR 作业请求,指定场景草稿、原图素材和对应模板。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:媒体作业接口响应模型。
        scene = await content_admin().get_scene(payload.scene_id)
        revision = await content_admin().get_revision(payload.revision_id)
        if payload.template_id != scene.template_type:
            raise AppError("OCR_TEMPLATE_MISMATCH", "OCR模板与场景模板不一致", 422)
        if (
            scene.draft_revision_id != revision.id
            or revision.scene_id != scene.id
            or revision.status != "DRAFT"
            or revision.content.get("original_image_asset_id") != payload.asset_id
        ):
            raise AppError("OCR_DRAFT_MISMATCH", "OCR需要当前草稿的学习原图", 409)
        asset = await service.get_asset(payload.asset_id)
        if asset.asset_type != "images" or asset.created_by != str(admin.admin_user_id):
            raise AppError("OCR_OBJECT_INVALID", "OCR须使用当前管理员上传的学习原图", 422)
        if payload.object_key is not None and payload.object_key != asset.object_key:
            raise AppError("OCR_OBJECT_INVALID", "OCR对象键与已确认素材不一致", 422)
        data = await service.read_asset_bytes(asset)
        await validate_ocr_image(data)
        inputs = payload.model_dump() | {
            "object_key": asset.object_key,
            "template_id": scene.template_type,
        }
        job = await media_admin().create_job(
            business_key=f"ocr:{admin.admin_user_id}:{idempotency_key}",
            job_type="OCR",
            target_id=asset.id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
            input_payload=inputs,
        )
        if job.status == "PENDING":
            await quota().reserve(job.id, clock())
            await dispatcher().enqueue_ocr(job.id, asset.object_key, scene.template_type)
        return _job_response(job)

    @router.get("/ocr/jobs/{job_id}", response_model=ProcessingJobResponse)
    async def get_ocr_job(
        job_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> ProcessingJobResponse:
        # 功能:读取 OCR 作业并转换为接口响应。
        # 参数:
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:媒体作业接口响应模型。
        return _job_response(await media_admin().get_job(job_id))

    @router.get("/ocr/jobs/{job_id}/candidate", response_model=OcrCandidateResponse)
    async def get_ocr_candidate(
        job_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> OcrCandidateResponse:
        # 功能:读取指定 OCR 作业的识别候选。
        # 参数:
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:OCR 候选接口响应模型。
        return _candidate_response(await media_admin().get_ocr_candidate(job_id))

    @router.post("/ocr/jobs/{job_id}/commands/{operation}")
    async def command_ocr_job(
        job_id: str,
        operation: str,
        payload: OcrCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse | OcrConfirmationResponse:
        # 功能:取消或重试 OCR 作业,要求候选确认通过草稿字段采纳流程。
        # 参数:
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     operation: OCR 命令名称,支持重试、取消和候选确认。
        #     payload: OCR 作业命令内容,候选确认时指定采纳的草稿版本。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:取消或重试后的作业响应;确认命令要求通过草稿字段采纳流程执行并抛出错误。
        media = media_admin()
        if operation == "cancel":
            job = await media.cancel_job(job_id, now=clock())
            await audit(
                request, admin, "media.ocr.cancel", "processing_job", job.id, {"status": job.status}
            )
            return _job_response(job)
        if operation == "retry":
            original = await media.get_job(job_id)
            if original.status not in {"FAILED", "CANCELLED"}:
                raise AppError("MEDIA_JOB_NOT_RETRYABLE", "当前媒体任务不可重试", 409)
            scene_id = original.input_payload.get("scene_id")
            revision_id = original.input_payload.get("revision_id")
            if not isinstance(scene_id, str) or not isinstance(revision_id, str):
                raise AppError("MEDIA_JOB_INPUT_INVALID", "媒体任务输入不完整", 409)
            scene = await content_admin().get_scene(scene_id)
            revision = await content_admin().get_revision(revision_id)
            if scene.draft_revision_id != revision.id or revision.scene_id != scene.id:
                raise AppError("OCR_DRAFT_MISMATCH", "OCR需要当前场景草稿", 409)
            if original.input_payload.get("template_id") != scene.template_type:
                raise AppError("OCR_TEMPLATE_MISMATCH", "OCR模板与场景模板不一致", 422)
            asset_id = revision.content.get("original_image_asset_id")
            if asset_id != original.target_id:
                raise AppError("OCR_DRAFT_MISMATCH", "OCR原图已变化", 409)
            asset = await service.get_asset(original.target_id)
            retried = await media.create_job(
                business_key=f"retry:{original.id}:{idempotency_key}",
                job_type=original.job_type,
                target_id=original.target_id,
                actor_id=str(admin.admin_user_id),
                now=clock(),
                batch_id=original.batch_id,
                input_payload=original.input_payload | {"object_key": asset.object_key},
            )
            object_key = asset.object_key
            template_id = scene.template_type
            if not object_key or not template_id:
                raise AppError("MEDIA_JOB_INPUT_INVALID", "媒体任务输入不完整", 409)
            await quota().reserve(retried.id, clock())
            await dispatcher().enqueue_ocr(retried.id, object_key, template_id)
            await audit(
                request,
                admin,
                "media.ocr.retry",
                "processing_job",
                retried.id,
                {"status": retried.status},
            )
            return _job_response(retried)
        if operation != "confirm":
            raise AppError("MEDIA_JOB_OPERATION_INVALID", "媒体任务操作不支持", 422)
        raise AppError("OCR_ADOPTION_REQUIRED", "请在指定草稿版本中选择字段采纳OCR候选", 409)

    @router.post("/audio-targets", status_code=201, response_model=AudioTargetResponse)
    async def create_audio_target(
        payload: CreateAudioTargetRequest,
        _admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _key: IdempotencyKey,
    ) -> AudioTargetResponse:
        # 功能:按稳定业务键和目标类型创建或复用音频目标。
        # 参数:
        #     payload: 音频目标请求,指定稳定业务键及音频用途类型。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     _key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:音频目标接口响应模型。
        return _audio_target_response(
            await media_admin().create_audio_target(payload.stable_key, payload.target_type)
        )

    @router.get("/audio-targets", response_model=AudioTargetListResponse)
    async def list_audio_targets(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
        scene_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> AudioTargetListResponse:
        # 功能:读取音频目标列表。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:音频目标列表接口响应模型。
        targets = await media_admin().list_audio_targets()
        if scene_id is not None:
            scene = await content_admin().get_scene(scene_id)
            target_ids: set[str] = set()
            whole_target_ids: set[str] = set()
            entry_ids: set[str] = set()
            for revision_id in {scene.draft_revision_id, scene.published_revision_id} - {None}:
                assert revision_id is not None
                revision = await content_admin().get_revision(revision_id)
                whole_audio = revision.content.get("audio")
                if isinstance(whole_audio, dict) and isinstance(whole_audio.get("target_id"), str):
                    whole_target_ids.add(whole_audio["target_id"])
                for section in ("vocabulary", "chunks"):
                    entries = revision.content.get(section, [])
                    if isinstance(entries, list):
                        for entry in entries:
                            if isinstance(entry, dict):
                                if isinstance(entry.get("audio_target_id"), str):
                                    target_ids.add(entry["audio_target_id"])
                                if isinstance(entry.get("entry_id"), str):
                                    entry_ids.add(entry["entry_id"])
            targets = [
                item
                for item in targets
                if (
                    item.target_type == "scene"
                    and (item.stable_key == scene_id or item.id in whole_target_ids)
                )
                or (
                    item.target_type in {"vocabulary", "chunk"}
                    and (item.id in target_ids or item.stable_key in entry_ids)
                )
            ]
        return AudioTargetListResponse(items=[_audio_target_response(item) for item in targets])

    @router.get(
        "/audio-targets/{target_id}/versions",
        response_model=AudioVersionListResponse,
    )
    async def list_audio_versions(
        target_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> AudioVersionListResponse:
        # 功能:读取指定音频目标的全部版本。
        # 参数:
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:音频版本列表接口响应模型。
        versions = await media_admin().list_audio_versions(target_id)
        return AudioVersionListResponse(items=[_audio_version_response(item) for item in versions])

    @router.post(
        "/audio-targets/{target_id}/versions",
        status_code=201,
        response_model=AudioVersionResponse,
    )
    async def create_audio_version(
        target_id: str,
        payload: CreateAudioVersionRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> AudioVersionResponse:
        # 功能:校验已确认音频素材后登记新的候选音频版本。
        # 参数:
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     payload: 音频候选版本请求,指定已确认素材、来源和可选生成请求关联。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:音频版本接口响应模型。
        media = media_admin()
        asset = await service.get_asset(payload.asset_id)
        if asset.asset_type != "audio" or not asset.duration_ms:
            raise AppError("AUDIO_ASSET_INVALID", "音频版本需要已检查的音频素材", 422)
        target = await media.get_audio_target(target_id)
        version = await media.create_audio_candidate(
            stable_key=target.stable_key,
            target_type=target.target_type,
            asset_id=payload.asset_id,
            source="MANUAL",
            actor_id=str(admin.admin_user_id),
            now=clock(),
            provider_request_id=f"manual:{target.id}:{idempotency_key}",
        )
        await audit(
            request,
            admin,
            "media.audio.upload",
            "audio_version",
            version.id,
            {"status": version.status},
        )
        return _audio_version_response(version)

    @router.post(
        "/audio-targets/{target_id}/commands/generate",
        status_code=201,
        response_model=ProcessingJobResponse,
    )
    async def generate_audio(
        target_id: str,
        payload: GenerateAudioRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> ProcessingJobResponse:
        # 功能:拒绝已禁用的语音生成请求,保留接口输入约定。
        # 参数:
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     payload: 语音生成请求,包含合成文本和声线标识。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:无正常返回;当前接口始终抛出语音生成已禁用的业务错误。
        raise AppError("TTS_DISABLED", "本期仅支持人工上传音频", 409)

    @router.post(
        "/audio-versions/{version_id}/commands/confirm",
        response_model=AudioTargetResponse,
    )
    async def confirm_audio_version(
        version_id: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> AudioTargetResponse:
        # 功能:确认候选音频版本并更新目标的生效版本。
        # 参数:
        #     version_id: 音频版本公开标识,定位待确认或回滚的素材版本。
        #     _payload: 空命令请求体,保持确认、取消或回收站命令接口的输入约定。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     _idempotency_key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:音频目标接口响应模型。
        target = await media_admin().confirm_audio_version(
            version_id, actor_id=str(admin.admin_user_id), now=clock()
        )
        await audit(
            request,
            admin,
            "media.audio.confirm",
            "audio_target",
            target.id,
            {"active_version_id": target.active_version_id or ""},
        )
        return _audio_target_response(target)

    @router.post(
        "/audio-targets/{target_id}/commands/rollback",
        response_model=AudioTargetResponse,
    )
    async def rollback_audio_version(
        target_id: str,
        payload: RollbackAudioRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> AudioTargetResponse:
        # 功能:核对版本归属并将音频目标回滚至指定已确认版本。
        # 参数:
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     payload: 音频回滚请求,指定需要恢复的历史音频版本。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     _idempotency_key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:音频目标接口响应模型。
        target = await media_admin().rollback_audio_version(
            target_id,
            payload.version_id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit(
            request,
            admin,
            "media.audio.rollback",
            "audio_target",
            target.id,
            {"active_version_id": target.active_version_id or ""},
        )
        return _audio_target_response(target)

    @router.get("/batch-jobs", response_model=BatchJobPageResponse)
    async def list_batch_jobs(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> BatchJobPageResponse:
        # 功能:分页返回内容批任务及执行统计。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:批任务分页接口响应模型。
        batches = await media_admin().list_batches()
        start = (page - 1) * page_size
        return BatchJobPageResponse(
            items=[
                await _batch_response(media_admin(), item)
                for item in batches[start : start + page_size]
            ],
            page=page,
            page_size=page_size,
            total=len(batches),
        )

    @router.post("/batch-jobs", status_code=201, response_model=BatchJobResponse)
    async def create_batch_job(
        payload: CreateBatchJobRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> BatchJobResponse:
        # 功能:按幂等请求创建内容批任务和任务项并发送至队列。
        # 参数:
        #     payload: 批任务创建请求,指定操作类型、目标列表和批量操作字段。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:批任务及其任务项的接口响应模型。
        if payload.job_type not in BATCH_OPERATIONS:
            raise AppError("BATCH_OPERATION_INVALID", "本期批量操作不支持或资源生成已禁用", 422)
        batch = await media_admin().create_batch(
            business_key=f"batch:{admin.admin_user_id}:{idempotency_key}",
            job_type=payload.job_type,
            target_ids=tuple(payload.target_ids),
            actor_id=str(admin.admin_user_id),
            now=clock(),
            input_payload=payload.input_payload,
        )
        if batch.status == "PENDING":
            await dispatcher().enqueue_batch(batch.id)
        await audit(
            request, admin, "media.batch.create", "batch_job", batch.id, {"status": batch.status}
        )
        return await _batch_response(media_admin(), batch)

    @router.get("/batch-jobs/{batch_id}", response_model=BatchJobResponse)
    async def get_batch_job(
        batch_id: str,
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> BatchJobResponse:
        # 功能:读取批任务及其任务项的接口响应。
        # 参数:
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:批任务及其任务项的接口响应模型。
        media = media_admin()
        return await _batch_response(media, await media.get_batch(batch_id))

    @router.post("/batch-jobs/{batch_id}/commands/{operation}", response_model=BatchJobResponse)
    async def command_batch_job(
        batch_id: str,
        operation: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: IdempotencyKey,
    ) -> BatchJobResponse:
        # 功能:执行批任务的取消或重试命令并记录审计。
        # 参数:
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     operation: 批任务命令名称,支持取消和重试。
        #     _payload: 空命令请求体,保持确认、取消或回收站命令接口的输入约定。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        # 返回:批任务及其任务项的接口响应模型。
        media = media_admin()
        if operation == "cancel":
            batch = await media.cancel_batch(batch_id, now=clock())
            action = "media.batch.cancel"
        elif operation == "retry-failed":
            original = await media.get_batch(batch_id)
            items = await media.list_batch_items(batch_id)
            target_ids = tuple(item.target_id for item in items if item.status == "FAILED")
            if not target_ids:
                raise AppError("BATCH_RETRY_EMPTY", "批量任务没有可重试失败项", 409)
            batch = await media.create_batch(
                business_key=f"batch-retry:{original.id}:{idempotency_key}",
                job_type=original.job_type,
                target_ids=target_ids,
                actor_id=str(admin.admin_user_id),
                now=clock(),
                input_payload=original.input_payload,
            )
            await dispatcher().enqueue_batch(batch.id)
            action = "media.batch.retry"
        else:
            raise AppError("BATCH_OPERATION_INVALID", "批量任务操作不支持", 422)
        await audit(request, admin, action, "batch_job", batch.id, {"status": batch.status})
        return await _batch_response(media, batch)

    @router.get("/trash", response_model=TrashListResponse)
    async def list_trash(
        _admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> TrashListResponse:
        # 功能:返回草稿回收站记录列表。
        # 参数:
        #     _admin: 通过对应读写权限依赖校验的管理员会话,保证接口访问权限。
        # 返回:回收站草稿列表接口响应模型。
        return TrashListResponse(
            items=[_trash_response(item) for item in await media_admin().list_trash_entries()]
        )

    @router.post("/trash", status_code=201, response_model=TrashEntryResponse)
    async def create_trash_entry(
        payload: CreateTrashRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> TrashEntryResponse:
        # 功能:校验未发布草稿并移入回收站,设置保留期限。
        # 参数:
        #     payload: 草稿回收请求,指定场景及尚未发布的草稿版本。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     _idempotency_key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:回收站草稿接口响应模型。
        entry = await media_admin().trash_draft(
            payload.scene_id,
            payload.revision_id,
            actor_id=str(admin.admin_user_id),
            now=clock(),
        )
        await audit(
            request, admin, "media.trash.create", "trash_entry", entry.id, {"status": entry.status}
        )
        return _trash_response(entry)

    @router.post(
        "/trash/{entry_id}/commands/{operation}",
        response_model=TrashEntryResponse,
    )
    async def command_trash_entry(
        entry_id: str,
        operation: str,
        _payload: EmptyCommandRequest,
        request: Request,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        _idempotency_key: IdempotencyKey,
    ) -> TrashEntryResponse:
        # 功能:执行回收站草稿的恢复或保留期后清理命令。
        # 参数:
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     operation: 回收站命令名称,支持恢复和保留期后清理。
        #     _payload: 空命令请求体,保持确认、取消或回收站命令接口的输入约定。
        #     request: 当前 HTTP 请求,提取请求追踪标识用于操作审计。
        #     admin: 通过鉴权的管理员会话,读取当前操作人的用户标识。
        #     _idempotency_key: 已由请求头校验的幂等键,当前处理器保留该接口约定。
        # 返回:回收站草稿接口响应模型。
        media = media_admin()
        if operation == "restore":
            entry = await media.restore_draft(
                entry_id, actor_id=str(admin.admin_user_id), now=clock()
            )
            action = "media.trash.restore"
        elif operation == "cleanup":
            entry = await media.cleanup_draft(
                entry_id, actor_id=str(admin.admin_user_id), now=clock()
            )
            action = "media.trash.cleanup"
        else:
            raise AppError("TRASH_OPERATION_INVALID", "回收站操作不支持", 422)
        await audit(request, admin, action, "trash_entry", entry.id, {"status": entry.status})
        return _trash_response(entry)

    return router


# 匿名函数: 提供可注入时钟的当前 UTC 时间。
# 参数: 无。
# 返回: 带 UTC 时区的当前时间。
def create_internal_media_router(
    service: MediaService,
    *,
    resolve_target: SignedTargetResolver,
    current_service: Callable[..., Awaitable[object]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能:注册内部服务访问音频签名的接口。
    # 参数:
    #     service: 素材服务,生成上传凭据、确认素材并签名资源访问。
    #     resolve_target: 按音频目标、用户和时间解析对象键及权益到期时间的异步回调。
    #     current_service: 内部服务身份验证依赖,限制媒体签名接口的调用方。
    #     clock: 返回当前时间的可注入时钟,供审计、签名和作业状态更新。
    # 返回:已注册对应业务接口和权限依赖的 FastAPI 路由器。
    router = APIRouter(prefix="/internal/v1/media", tags=["internal-media"])

    @router.get("/{target_id}/signed-url")
    async def signed_url(
        target_id: str,
        user_id: Annotated[str, Header(alias="X-User-ID")],
        _principal: Annotated[object, Depends(current_service)],
    ) -> dict[str, object]:
        # 功能:校验内部服务身份后解析音频访问权限并生成签名。
        # 参数:
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     user_id: 访问学习内容的用户公开标识,供权益查询和访问授权。
        #     _principal: 通过内部服务鉴权依赖校验的调用身份。
        # 返回:音频访问 URL 和签名到期时间。
        now = clock()
        object_key, entitlement_expires_at = await resolve_target(target_id, user_id, now)
        signed = await service.sign_media(object_key, entitlement_expires_at, now)
        return {"url": signed.url, "expires_at": signed.expires_at}

    return router


def _job_response(job: ProcessingJob) -> ProcessingJobResponse:
    # 功能:将媒体处理作业转换为接口响应模型。
    # 参数:
    #     job: 媒体处理作业对象,包含输入、状态和供应商请求信息。
    # 返回:媒体作业接口响应模型。
    return ProcessingJobResponse(
        **{name: getattr(job, name) for name in ProcessingJobResponse.model_fields}
    )


def _candidate_response(candidate: OcrCandidate) -> OcrCandidateResponse:
    # 功能:将 OCR 候选转换为接口响应模型。
    # 参数:
    #     candidate: OCR 候选对象,包含原图、文本、结构化块和采纳状态。
    # 返回:OCR 候选接口响应模型。
    return OcrCandidateResponse(
        **{name: getattr(candidate, name) for name in OcrCandidateResponse.model_fields}
    )


def _audio_target_response(target: AudioTarget) -> AudioTargetResponse:
    # 功能:将音频目标转换为接口响应模型。
    # 参数:
    #     target: 稳定音频目标对象,包含业务键、类型和生效版本引用。
    # 返回:音频目标接口响应模型。
    return AudioTargetResponse(
        **{name: getattr(target, name) for name in AudioTargetResponse.model_fields}
    )


def _audio_version_response(version: AudioVersion) -> AudioVersionResponse:
    # 功能:将音频版本转换为接口响应模型。
    # 参数:
    #     version: 音频版本对象,包含素材引用、来源和确认状态。
    # 返回:音频版本接口响应模型。
    return AudioVersionResponse(
        **{name: getattr(version, name) for name in AudioVersionResponse.model_fields}
    )


def _batch_item_response(item: BatchJobItem) -> BatchJobItemResponse:
    # 功能:将批任务项转换为接口响应模型。
    # 参数:
    #     item: 批任务项对象,包含目标、执行次数和结果状态。
    # 返回:批任务项接口响应模型。
    return BatchJobItemResponse(
        **{name: getattr(item, name) for name in BatchJobItemResponse.model_fields}
    )


async def _batch_response(service: MediaAdminService, batch: BatchJob) -> BatchJobResponse:
    # 功能:读取批任务项并组合批任务接口响应模型。
    # 参数:
    #     service: 媒体管理服务,读取和更新批任务及其任务项。
    #     batch: 批任务对象,包含目标数量、执行统计和状态。
    # 返回:批任务及其任务项的接口响应模型。
    values = {
        name: getattr(batch, name) for name in BatchJobResponse.model_fields if name != "items"
    }
    values["items"] = [
        _batch_item_response(item) for item in await service.list_batch_items(batch.id)
    ]
    return BatchJobResponse(**values)


def _trash_response(entry: TrashEntry) -> TrashEntryResponse:
    # 功能:将回收站草稿记录转换为接口响应模型。
    # 参数:
    #     entry: 回收站草稿对象,包含场景版本、保留期和恢复清理状态。
    # 返回:回收站草稿接口响应模型。
    return TrashEntryResponse(
        **{name: getattr(entry, name) for name in TrashEntryResponse.model_fields}
    )


def _asset_response(asset: MediaAsset) -> dict[str, object]:
    # 功能:将素材记录转换为包含尺寸、时长和审核状态的接口字段。
    # 参数:
    #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
    # 返回:素材标识、对象键、尺寸或时长及确认审核状态字段。
    return {
        "id": asset.id,
        "asset_type": asset.asset_type,
        "content_type": asset.content_type,
        "size": asset.size,
        "sha256": asset.sha256,
        "status": asset.status,
        "security_status": asset.security_status,
        "width": asset.width,
        "height": asset.height,
        "duration_ms": asset.duration_ms,
        "security_request_id": asset.security_request_id,
    }
