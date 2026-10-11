import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.infrastructure.tasks.celery_app import celery_app
from juya_admin_api.integrations.ocr.protocol import OcrProvider, OcrResult
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider
from juya_admin_api.integrations.tts.protocol import TtsProvider, TtsResult
from juya_admin_api.modules.media.domain import ProcessingJob as PersistentProcessingJob
from juya_admin_api.modules.media.quota import OcrQuotaService
from juya_admin_api.modules.media.repository import (
    SQLAlchemyMediaAdminRepository,
    SQLAlchemyMediaRepository,
)
from juya_admin_api.modules.media.service import (
    MediaAdminRepository,
    MediaAdminService,
    MediaAsset,
    MediaService,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@dataclass(frozen=True, slots=True)
class ProcessingJob:
    id: str
    business_key: str
    job_type: str
    target_id: str
    status: str
    provider_request_id: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AudioTarget:
    target_id: str
    object_key: str
    source: str
    revision: int


@dataclass(frozen=True, slots=True)
class BatchJob:
    id: str
    business_key: str
    job_type: str
    status: str
    total_count: int
    success_count: int
    failure_count: int
    created_at: datetime


class MediaJobRepository(Protocol):
    async def get_job(self, business_key: str) -> ProcessingJob | None:
        # 功能:读取媒体处理作业及供应商调用状态。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        ...

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        # 功能:保存媒体处理作业及其幂等业务键。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job: 媒体处理作业对象,包含输入、状态和供应商请求信息。
        # 返回:媒体作业及执行状态。
        ...

    async def save_ocr_candidate(self, job_id: str, result: OcrResult) -> None:
        # 功能:保存 OCR 文本、结构化识别块及采纳状态。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     result: OCR 供应商结果,包含识别文本、结构化块和请求标识。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def save_tts_candidate(self, job_id: str, target_id: str, result: TtsResult) -> None:
        # 功能:保存语音生成候选,保留已有人工音频优先级。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     result: 语音生成供应商结果,包含生成对象键和请求标识。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def get_batch(self, business_key: str) -> BatchJob | None:
        # 功能:读取批任务及执行统计。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        ...

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        # 功能:保存批任务状态和执行统计。
        # 参数:
        #     self: 当前 MediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        # 返回:批任务及执行统计和当前状态。
        ...


class InMemoryMediaJobRepository:
    def __init__(self) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.jobs: dict[str, ProcessingJob] = {}
        self.ocr_candidates: dict[str, OcrResult] = {}
        self.tts_candidates: dict[str, TtsResult] = {}
        self.audio_targets: dict[str, AudioTarget] = {}
        self.batches: dict[str, BatchJob] = {}

    async def get_job(self, business_key: str) -> ProcessingJob | None:
        # 功能:读取媒体处理作业及供应商调用状态。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        return self.jobs.get(business_key)

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        # 功能:保存媒体处理作业及其幂等业务键。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job: 媒体处理作业对象,包含输入、状态和供应商请求信息。
        # 返回:媒体作业及执行状态。
        self.jobs[job.business_key] = job
        return job

    async def save_ocr_candidate(self, job_id: str, result: OcrResult) -> None:
        # 功能:保存 OCR 文本、结构化识别块及采纳状态。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     result: OCR 供应商结果,包含识别文本、结构化块和请求标识。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.ocr_candidates[job_id] = result

    async def save_tts_candidate(self, job_id: str, target_id: str, result: TtsResult) -> None:
        # 功能:保存语音生成候选,保留已有人工音频优先级。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     result: 语音生成供应商结果,包含生成对象键和请求标识。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.tts_candidates[job_id] = result
        current = self.audio_targets.get(target_id)
        if current is None or current.source != "MANUAL":
            revision = 1 if current is None else current.revision + 1
            self.audio_targets[target_id] = AudioTarget(
                target_id, result.object_key, "TTS", revision
            )

    async def get_batch(self, business_key: str) -> BatchJob | None:
        # 功能:读取批任务及执行统计。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        return self.batches.get(business_key)

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        # 功能:保存批任务状态和执行统计。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        # 返回:批任务及执行统计和当前状态。
        self.batches[batch.business_key] = batch
        return batch

    def set_manual_audio(self, target_id: str, object_key: str, revision: int) -> None:
        # 功能:登记音频目标的人工上传版本以防自动生成结果覆盖。
        # 参数:
        #     self: 当前 InMemoryMediaJobRepository 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     revision: 人工音频版本序号,记录该目标当前人工版本并与生成版本区分。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.audio_targets[target_id] = AudioTarget(target_id, object_key, "MANUAL", revision)


class MediaTaskService:
    def __init__(
        self,
        repository: MediaJobRepository,
        ocr: OcrProvider,
        tts: TtsProvider,
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 MediaTaskService 实例,持有本方法访问的依赖和业务状态。
        #     repository: 媒体任务仓储,保存作业、识别和语音候选及批任务状态。
        #     ocr: OCR 供应商,识别素材图片并返回文本及结构化识别块。
        #     tts: 语音合成供应商,按文本和声线生成候选音频。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._repository = repository
        self._ocr = ocr
        self._tts = tts

    async def run_tts(
        self,
        business_key: str,
        target_id: str,
        text: str,
        voice: str,
        now: datetime,
    ) -> ProcessingJob:
        # 功能:保留已有终态作业,否则将语音生成作业标记为 TTS_DISABLED 失败。
        # 参数:
        #     self: 当前 MediaTaskService 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     text: 需要生成语音的原始文本。
        #     voice: 语音合成声线标识,传递给合成供应商。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:已有终态作业或写入 TTS_DISABLED 失败状态后的作业。
        existing = await self._repository.get_job(business_key)
        if existing is not None:
            return existing
        return await self._repository.save_job(
            ProcessingJob(
                new_ulid(now),
                business_key,
                "TTS",
                target_id,
                "FAILED",
                None,
                "TTS_DISABLED",
                now,
                now,
            )
        )

    async def run_tts_batch(
        self,
        business_key: str,
        items: tuple[tuple[str, str, str], ...],
        now: datetime,
    ) -> BatchJob:
        # 功能:逐项生成音频,按目标汇总批任务成功和失败数。
        # 参数:
        #     self: 当前 MediaTaskService 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        #     items: 语音批生成项元组,每项依次包含目标标识、合成文本和声线标识。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:批任务及执行统计和当前状态。
        existing = await self._repository.get_batch(business_key)
        if existing is not None:
            return existing
        success_count = 0
        for index, (target_id, text, voice) in enumerate(items):
            item = await self.run_tts(
                f"{business_key}:{index}:{target_id}", target_id, text, voice, now
            )
            success_count += item.status == "SUCCEEDED"
        batch = BatchJob(
            id=new_ulid(now),
            business_key=business_key,
            job_type="TTS",
            status="COMPLETED" if success_count == len(items) else "COMPLETED_WITH_ERRORS",
            total_count=len(items),
            success_count=success_count,
            failure_count=len(items) - success_count,
            created_at=now,
        )
        return await self._repository.save_batch(batch)


class PersistentMediaTaskService:
    def __init__(
        self,
        admin_service: MediaAdminService,
        repository: MediaAdminRepository,
        ocr: OcrProvider,
        tts: TtsProvider,
        *,
        register_generated_audio: Callable[[TtsResult, datetime], Awaitable[str]],
        quota: OcrQuotaService | None = None,
        media_service: MediaService | None = None,
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 PersistentMediaTaskService 实例,持有本方法访问的依赖和业务状态。
        #     admin_service: 媒体管理服务,创建和更新作业、音频版本及批任务。
        #     repository: 媒体管理仓储,管理作业、批任务、音频版本和草稿回收。
        #     ocr: OCR 供应商,识别素材图片并返回文本及结构化识别块。
        #     tts: 语音合成供应商,按文本和声线生成候选音频。
        #     register_generated_audio: 接收生成结果和操作时间、登记素材后返回素材标识的异步回调。
        #     quota: OCR 额度服务,调用供应商前预占已核实的免费额度。
        #     media_service: 素材服务,核对原图字节和固定对象引用;允许未配置。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._admin_service = admin_service
        self._repository = repository
        self._ocr = ocr
        self._tts = tts
        self._register_generated_audio = register_generated_audio
        self._quota = quota
        self._media_service = media_service

    async def run_tts(
        self,
        job_id: str,
        *,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
        now: datetime,
    ) -> PersistentProcessingJob:
        # 功能:保留已有终态作业,否则将语音生成作业标记为 TTS_DISABLED 失败。
        # 参数:
        #     self: 当前 PersistentMediaTaskService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;
        #         兼容大写输入。
        #     text: 需要生成语音的原始文本。
        #     voice: 语音合成声线标识,传递给合成供应商。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:已有终态作业或写入 TTS_DISABLED 失败状态后的作业。
        job = await self._admin_service.get_job(job_id)
        if job.status in {"SUCCEEDED", "CANCELLED", "FAILED"}:
            return job
        return await self._admin_service.save_job_result(
            job.id, status="FAILED", provider_request_id=None, error_code="TTS_DISABLED", now=now
        )


class CeleryMediaTaskDispatcher:
    def __init__(self, *, enabled: bool) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 CeleryMediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     enabled: 是否启用 OCR 调用或后台任务调度,具体由当前实例策略决定。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._enabled = enabled

    async def enqueue_batch(self, batch_id: str) -> None:
        # 功能:将内容批任务发送到工作队列。
        # 参数:
        #     self: 当前 CeleryMediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        celery_app.send_task("juya.content.publish.batch_execute", kwargs={"batch_id": batch_id})

    def _require_enabled(self) -> None:
        # 功能:检查后台队列已启用,未启用时拒绝调度。
        # 参数:
        #     self: 当前 CeleryMediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        if not self._enabled:
            raise AppError(
                "MEDIA_TASKS_UNAVAILABLE",
                "当前环境未配置 OCR/TTS 任务提供方",
                503,
            )

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None:
        # 功能:拒绝本期已禁用的语音生成任务调度。
        # 参数:
        #     self: 当前 CeleryMediaTaskDispatcher 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;
        #         兼容大写输入。
        #     text: 需要生成语音的原始文本。
        #     voice: 语音合成声线标识,传递给合成供应商。
        # 返回:无正常返回;始终抛出语音生成已禁用的业务错误。
        raise AppError("TTS_DISABLED", "本期仅支持人工上传音频, TTS执行已禁用", 409)


class LocalOcrProvider:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能:根据本地对象键生成确定性的 OCR 测试候选。
        # 参数:
        #     self: 当前 LocalOcrProvider 实例,持有本方法访问的依赖和业务状态。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     template_type: 内容模板类型,区分 dialogue 对话与 vocabulary 词汇。
        # 返回:OCR 文本及结构化识别结果。
        digest = hashlib.sha256(f"{object_key}:{template_type}".encode()).hexdigest()[:24]
        return OcrResult(
            provider_request_id=f"local-ocr-{digest}",
            text=f"recognized:{object_key}:{template_type}",
            blocks=[
                {
                    "type": "text",
                    "text": f"recognized:{object_key}",
                    "confidence": 1.0,
                }
            ],
        )


class LocalTtsProvider:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        # 功能:根据目标、声线和文本生成本地语音测试结果。
        # 参数:
        #     self: 当前 LocalTtsProvider 实例,持有本方法访问的依赖和业务状态。
        #     audio_target: 本地语音生成目标业务标识,参与确定性对象键计算。
        #     voice: 语音合成声线标识,传递给合成供应商。
        #     text: 需要生成语音的原始文本。
        # 返回:语音生成的对象键及供应商追踪信息。
        digest = hashlib.sha256(f"{audio_target}:{voice}:{text}".encode()).hexdigest()
        return TtsResult(
            provider_request_id=f"local-tts-{digest[:24]}",
            object_key=f"generated/audio/{digest}.mp3",
            duration_ms=max(len(text) * 100, 100),
        )


@celery_app.task(name="juya.content.audio.generate")  # type: ignore[untyped-decorator]
def process_tts(
    job_id: str,
    stable_key: str,
    target_type: str,
    text: str,
    voice: str,
) -> dict[str, object]:
    # 功能:通过同步队列入口运行异步语音生成作业。
    # 参数:
    #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
    #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
    #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;兼容大写输入。
    #     text: 需要生成语音的原始文本。
    #     voice: 语音合成声线标识,传递给合成供应商。
    # 返回:语音作业标识、状态和供应商结果追踪字段。
    return asyncio.run(_process_tts(job_id, stable_key, target_type, text, voice))


async def _process_tts(
    job_id: str, stable_key: str, target_type: str, text: str, voice: str
) -> dict[str, object]:
    raise AppError("TTS_DISABLED", "旧整段语音生成已退役, 请使用受控词语发音", 409)


def _job_payload(job: PersistentProcessingJob) -> dict[str, object]:
    # 功能:提取媒体作业标识、状态和供应商错误的队列返回字段。
    # 参数:
    #     job: 持久化媒体作业对象,包含状态和供应商请求信息。
    # 返回:作业标识、状态、供应商请求标识和错误代码字段。
    return {
        "id": job.id,
        "status": job.status,
        "provider_request_id": job.provider_request_id,
        "error_code": job.error_code,
    }


@celery_app.task(name="juya.content.publish.batch_execute")  # type: ignore[untyped-decorator]
def process_batch(batch_id: str) -> dict[str, object]:
    # 功能:通过同步队列入口执行异步内容批任务。
    # 参数:
    #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
    # 返回:批任务标识、终态和成功失败统计。
    return asyncio.run(_process_batch(batch_id))


@celery_app.task(name="juya.content.publish.batch_recover")  # type: ignore[untyped-decorator]
def recover_batches() -> dict[str, object]:
    # 功能:通过同步队列入口重新调度可恢复的内容批任务。
    # 参数:无。
    # 返回:本次重新发送到队列的批任务数量。
    return asyncio.run(_recover_batches())


async def _recover_batches() -> dict[str, object]:
    # 功能:查找待处理及租约过期批任务并重新发送到队列。
    # 参数:无。
    # 返回:本次重新发送到队列的批任务数量。
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required for batch recovery")
    engine = create_engine(
        settings.database_url.get_secret_value().replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    )
    try:
        repository = SQLAlchemyMediaAdminRepository(create_session_factory(engine))
        pending = await repository.list_recoverable_batches(datetime.now(UTC))
        for batch_id in pending:
            process_batch.delay(batch_id)
        return {"enqueued": len(pending)}
    finally:
        await engine.dispose()


async def _process_batch(batch_id: str) -> dict[str, object]:
    # 功能:构造内容操作及审计依赖,执行批任务并释放数据库引擎。
    # 参数:
    #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
    # 返回:批任务标识、终态和成功失败统计。
    from juya_admin_api.modules.audit.service import (
        AuditEvent,
        AuditService,
        SQLAlchemyAuditRepository,
    )
    from juya_admin_api.modules.content.batch_executor import BatchExecutor, ContentBatchOperations
    from juya_admin_api.modules.content.production_store import ProductionStore
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.service import ContentService

    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required for batch tasks")
    engine = create_engine(
        settings.database_url.get_secret_value().replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    )
    sessions = create_session_factory(engine)
    repository = SQLAlchemyMediaAdminRepository(sessions)
    admin = MediaAdminService(repository)

    async def prepare_asset(asset_id: str) -> MediaAsset:
        # 功能:按批任务配置读取素材并准备不可变对象引用。
        # 参数:
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        # 返回:素材记录及尺寸、时长和审核状态。
        settings.validate_oss_configuration()
        assert settings.oss_region is not None and settings.oss_bucket is not None
        oss = AliyunOssProvider(
            settings.oss_region,
            settings.oss_bucket,
            endpoint=settings.oss_endpoint,
            credentials_provider=ControlledCredentialsProvider(
                mode=settings.oss_credentials_mode,
                role_name=settings.oss_ram_role_name,
                access_key_id=settings.oss_access_key_id.get_secret_value()
                if settings.oss_access_key_id
                else None,
                access_key_secret=settings.oss_access_key_secret.get_secret_value()
                if settings.oss_access_key_secret
                else None,
                security_token=settings.oss_session_token.get_secret_value()
                if settings.oss_session_token
                else None,
                expires_at=settings.oss_credentials_expires_at,
                from_environment=True,
            ),
        )
        media = MediaService(
            oss,
            SQLAlchemyMediaRepository(sessions),
            ffprobe_path=settings.ffprobe_path,
            require_review=settings.content_security_enabled,
        )
        return await media.get_asset(asset_id)

    content = ContentService(
        SQLAlchemyContentRepository(
            sessions,
            require_review=settings.content_security_enabled,
            prepare_asset=prepare_asset,
        )
    )
    batch = await admin.get_batch(batch_id)

    async def audit(kind: str, target: str, result: dict[str, object], now: datetime) -> None:
        # 功能:记录操作人、业务对象和变更结果的审计事件。
        # 参数:
        #     kind: 内容批操作类型代码,决定校验、发布、OCR 或编辑等业务分支。
        #     target: 当前批操作的场景公开标识,定位目标场景及其草稿。
        #     result: 本次创建或批任务项执行结果字典,写入幂等回执或审计摘要。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        await AuditService(SQLAlchemyAuditRepository(sessions)).record(
            AuditEvent(
                actor_public_id=batch.created_by,
                action=f"media.batch.{kind.lower()}",
                object_type="scene",
                object_public_id=target,
                before_summary={},
                after_summary=result,
                reason=None,
                request_id=f"batch-{batch_id}",
                occurred_at=now,
            )
        )

    try:
        result = await BatchExecutor(
            admin,
            repository,
            ContentBatchOperations(
                content,
                ProductionStore(
                    sessions,
                    require_review=settings.content_security_enabled,
                    prepare_asset=prepare_asset,
                ),
            ),
            audit=audit,
        ).run(batch_id, datetime.now(UTC))
        return {
            "id": result.id,
            "status": result.status,
            "success_count": result.success_count,
            "failure_count": result.failure_count,
        }
    finally:
        await engine.dispose()
