import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.integrations.content_security.protocol import ContentSecurityProvider
from juya_admin_api.integrations.oss.provider import ObjectMetadata, OssProvider, UploadPolicy
from juya_admin_api.modules.media.domain import (
    AudioTarget,
    AudioVersion,
    BatchJob,
    BatchJobItem,
    OcrCandidate,
    ProcessingJob,
    TrashEntry,
)
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

IMAGE_MAX_BYTES = 20 * 1024 * 1024
AUDIO_MAX_BYTES = 50 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp"}
AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/aac",
}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac"}


@dataclass(frozen=True, slots=True)
class MediaAsset:
    id: str
    object_key: str
    asset_type: str
    content_type: str
    size: int
    sha256: str
    status: str
    security_status: str
    created_by: str
    created_at: datetime
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    security_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class SignedMedia:
    url: str = field(repr=False)
    expires_at: datetime


class MediaRepository(Protocol):
    async def get_by_object_key(self, object_key: str) -> MediaAsset | None:
        # 功能:按存储对象键读取素材登记记录。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        ...
    async def bind_fixed_object(self, asset: MediaAsset, object_key: str) -> MediaAsset:
        # 功能:将素材记录绑定到冻结后的不可变对象键。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        # 返回:素材记录及尺寸、时长和审核状态。
        ...
    async def get(self, asset_id: str) -> MediaAsset | None:
        # 功能:按公开标识读取素材记录。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        ...
    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        # 功能:按素材类型和内容摘要查找可去重的素材记录。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     sha256: 素材原始字节的 SHA-256 十六进制摘要,供内容去重。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        ...

    async def save(self, asset: MediaAsset) -> MediaAsset:
        # 功能:保存素材登记记录。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:素材记录及尺寸、时长和审核状态。
        ...

    async def update_security(self, asset: MediaAsset) -> MediaAsset:
        # 功能:保存素材审核状态、检测信息和可用状态。
        # 参数:
        #     self: 当前 MediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:素材记录及尺寸、时长和审核状态。
        ...


class InMemoryMediaRepository:
    def __init__(self) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.assets: dict[str, MediaAsset] = {}
        self.by_hash: dict[tuple[str, str], str] = {}

    async def get(self, asset_id: str) -> MediaAsset | None:
        # 功能:按公开标识读取素材记录。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        return self.assets.get(asset_id)

    async def get_by_object_key(self, object_key: str) -> MediaAsset | None:
        # 功能:按存储对象键读取素材登记记录。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        return next(
            (asset for asset in self.assets.values() if asset.object_key == object_key), None
        )

    async def bind_fixed_object(self, asset: MediaAsset, object_key: str) -> MediaAsset:
        # 功能:将素材记录绑定到冻结后的不可变对象键。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        # 返回:素材记录及尺寸、时长和审核状态。
        current = self.assets[asset.id]
        if current.object_key.startswith("sealed/media/"):
            return current
        current = replace(current, object_key=object_key)
        self.assets[asset.id] = current
        return current

    async def update_security(self, asset: MediaAsset) -> MediaAsset:
        # 功能:保存素材审核状态、检测信息和可用状态。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:素材记录及尺寸、时长和审核状态。
        self.assets[asset.id] = asset
        return asset

    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        # 功能:按素材类型和内容摘要查找可去重的素材记录。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     sha256: 素材原始字节的 SHA-256 十六进制摘要,供内容去重。
        # 返回:素材记录及尺寸、时长和审核状态;未找到对应记录时为 None。
        asset_id = self.by_hash.get((asset_type, sha256))
        return self.assets.get(asset_id) if asset_id else None

    async def save(self, asset: MediaAsset) -> MediaAsset:
        # 功能:保存素材登记记录。
        # 参数:
        #     self: 当前 InMemoryMediaRepository 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:素材记录及尺寸、时长和审核状态。
        existing = await self.get_by_hash(asset.asset_type, asset.sha256)
        if existing is not None:
            return existing
        self.assets[asset.id] = asset
        self.by_hash[(asset.asset_type, asset.sha256)] = asset.id
        return asset


class MediaAdminRepository(Protocol):
    async def create_batch_with_items(
        self, batch: BatchJob, items: list[BatchJobItem]
    ) -> BatchJob:
        # 功能:同时保存批任务及所有任务项,保证创建的一致性。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        #     items: 与批任务同时创建的任务项列表,包含各项目标及稳定任务键。
        # 返回:批任务及执行统计和当前状态。
        ...

    async def claim_batch(
        self, batch_id: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        # 功能:领取或恢复批任务租约,避免同一批任务被多个执行器处理。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        # 返回:是否成功获得批任务的执行租约。
        ...
    async def heartbeat_batch(self, batch_id: str, lease_token: str, now: datetime) -> bool:
        # 功能:核对执行器租约并延长批任务的有效租期。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:租约是否仍归当前执行器持有且已成功续租。
        ...
    async def list_recoverable_batches(self, now: datetime, limit: int = 100) -> list[str]:
        # 功能:查找待执行或租约过期的可恢复批任务。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     limit: 可恢复批任务的最大返回条数。
        # 返回:可恢复批任务的公开标识列表。
        ...
    async def finish_claimed_batch_item(
        self,
        batch_id: str,
        item_key: str,
        lease_token: str,
        result: dict[str, object],
        error_code: str | None,
        now: datetime,
    ) -> bool:
        # 功能:核对租约后提交任务项结果并汇总批任务执行状态。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     item_key: 批任务内任务项的稳定键,领取和回写结果时据此定位。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        #     result: 本次创建或批任务项执行结果字典,写入幂等回执或审计摘要。
        #     error_code: 执行失败的业务错误代码,成功时通常为空。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:租约是否有效且任务项结果已成功写入。
        ...
    async def claim_batch_item(
        self, batch_id: str, item_key: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        # 功能:核对批任务租约并领取尚未完成的任务项。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     item_key: 批任务内任务项的稳定键,领取和回写结果时据此定位。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        # 返回:是否成功领取该任务项。
        ...
    async def cancel_pending_batch_items(self, batch_id: str, now: datetime) -> None:
        # 功能:将批任务中尚未开始的任务项标记为取消。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def claim_job(self, job_id: str, now: datetime) -> bool:
        # 功能:原子领取待处理的媒体作业并切换为运行状态。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:是否成功将媒体作业从待处理状态切换为运行状态。
        ...

    async def get_job_by_business_key(self, business_key: str) -> ProcessingJob | None:
        # 功能:按幂等业务键读取媒体处理作业。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        ...

    async def get_job(self, job_id: str) -> ProcessingJob | None:
        # 功能:读取媒体处理作业及供应商调用状态。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        ...

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        # 功能:保存媒体处理作业及其幂等业务键。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job: 媒体处理作业对象,包含输入、状态和供应商请求信息。
        # 返回:媒体作业及执行状态。
        ...

    async def get_ocr_candidate_by_job(self, job_id: str) -> OcrCandidate | None:
        # 功能:读取指定 OCR 作业对应的识别候选。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:OCR 识别候选及采纳状态;未找到对应记录时为 None。
        ...

    async def save_ocr_candidate(self, candidate: OcrCandidate) -> OcrCandidate:
        # 功能:保存 OCR 文本、结构化识别块及采纳状态。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     candidate: OCR 候选对象,包含原图、文本、结构化块和采纳状态。
        # 返回:OCR 识别候选及采纳状态。
        ...

    async def get_batch_by_business_key(self, business_key: str) -> BatchJob | None:
        # 功能:按幂等业务键读取批任务。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        ...

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        # 功能:读取批任务及执行统计。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        ...

    async def list_batches(self) -> list[BatchJob]:
        # 功能:读取批任务列表。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:批任务及执行统计和当前状态的列表。
        ...

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        # 功能:保存批任务状态和执行统计。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        # 返回:批任务及执行统计和当前状态。
        ...

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        # 功能:读取批任务的所有任务项。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务项及执行结果的列表。
        ...

    async def save_batch_item(self, item: BatchJobItem) -> BatchJobItem:
        # 功能:保存单个批任务项的状态、错误和结果版本。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     item: 批任务项对象,包含目标、执行次数和结果状态。
        # 返回:批任务项及执行结果。
        ...

    async def get_audio_target(self, target_id: str) -> AudioTarget | None:
        # 功能:读取稳定音频目标及当前生效版本引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:稳定音频目标及生效版本引用;未找到对应记录时为 None。
        ...

    async def get_audio_target_by_stable_key(self, stable_key: str) -> AudioTarget | None:
        # 功能:按稳定业务键读取音频目标。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        # 返回:稳定音频目标及生效版本引用;未找到对应记录时为 None。
        ...

    async def save_audio_target(self, target: AudioTarget) -> AudioTarget:
        # 功能:保存音频目标和当前生效版本引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target: 稳定音频目标对象,包含业务键、类型和生效版本引用。
        # 返回:稳定音频目标及生效版本引用。
        ...

    async def list_audio_targets(self) -> list[AudioTarget]:
        # 功能:读取音频目标列表。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:稳定音频目标及生效版本引用的列表。
        ...

    async def get_audio_version(self, version_id: str) -> AudioVersion | None:
        # 功能:读取指定音频版本及素材引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     version_id: 音频版本公开标识,定位待确认或回滚的素材版本。
        # 返回:音频版本及其素材、来源和确认状态;未找到对应记录时为 None。
        ...

    async def find_audio_version_by_provider_request(
        self, provider_request_id: str
    ) -> AudioVersion | None:
        # 功能:按供应商请求标识查找已登记的音频版本。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     provider_request_id: 供应商调用请求标识,用于追踪、轮询或生成结果去重。
        # 返回:音频版本及其素材、来源和确认状态;未找到对应记录时为 None。
        ...

    async def save_audio_version(self, version: AudioVersion) -> AudioVersion:
        # 功能:保存音频版本、来源和生成作业关联。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     version: 音频版本对象,包含素材引用、来源和确认状态。
        # 返回:音频版本及其素材、来源和确认状态。
        ...

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        # 功能:读取指定音频目标的全部版本。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:音频版本及其素材、来源和确认状态的列表。
        ...

    async def get_trash_entry(self, entry_id: str) -> TrashEntry | None:
        # 功能:读取草稿回收站记录及保留期限。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        # 返回:草稿回收站记录及保留期限;未找到对应记录时为 None。
        ...

    async def get_trash_by_revision(self, revision_id: str) -> TrashEntry | None:
        # 功能:按内容版本读取草稿回收站记录。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:草稿回收站记录及保留期限;未找到对应记录时为 None。
        ...

    async def save_trash_entry(self, entry: TrashEntry) -> TrashEntry:
        # 功能:保存草稿回收站记录和恢复清理时间。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry: 回收站草稿对象,包含场景版本、保留期和恢复清理状态。
        # 返回:草稿回收站记录及保留期限。
        ...

    async def list_trash_entries(self) -> list[TrashEntry]:
        # 功能:读取草稿回收站记录列表。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:草稿回收站记录及保留期限的列表。
        ...

    async def is_draft_revision(self, scene_id: str, revision_id: str) -> bool:
        # 功能:判断指定内容版本是否为场景可回收的草稿。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:该版本是否是指定场景可回收的草稿。
        ...

    async def has_draft_references(self, revision_id: str) -> bool:
        # 功能:检查草稿是否被内容包、活动或其他内容版本引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:草稿是否仍被其他业务记录引用。
        ...

    async def purge_draft(self, scene_id: str, revision_id: str) -> None:
        # 功能:删除草稿并清除场景的草稿版本引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def transition_trash(
        self, entry_id: str, action: str, actor_id: str, now: datetime
    ) -> TrashEntry:
        # 功能:按恢复或清理命令迁移回收站状态,并校验保留期和业务引用。
        # 参数:
        #     self: 当前 MediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     action: 业务命令或审计动作代码,标识本次状态迁移或变更类型。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:草稿回收站记录及保留期限。
        ...


class InMemoryMediaAdminRepository:
    def __init__(self) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.jobs: dict[str, ProcessingJob] = {}
        self.ocr_candidates: dict[str, OcrCandidate] = {}
        self.batches: dict[str, BatchJob] = {}
        self.batch_by_business: dict[str, str] = {}
        self.batch_items: dict[tuple[str, str], BatchJobItem] = {}
        self.audio_targets: dict[str, AudioTarget] = {}
        self.audio_target_by_stable_key: dict[str, str] = {}
        self.audio_versions: dict[str, AudioVersion] = {}
        self.audio_version_by_provider_request: dict[str, str] = {}
        self.trash_entries: dict[str, TrashEntry] = {}
        self.trash_by_revision: dict[str, str] = {}
        self.drafts: set[tuple[str, str]] = set()
        self.referenced_drafts: set[str] = set()

    async def create_batch_with_items(self, batch: BatchJob, items: list[BatchJobItem]) -> BatchJob:
        # 功能:同时保存批任务及所有任务项,保证创建的一致性。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        #     items: 与批任务同时创建的任务项列表,包含各项目标及稳定任务键。
        # 返回:批任务及执行统计和当前状态。
        existing_id = self.batch_by_business.get(batch.business_key)
        if existing_id:
            return self.batches[existing_id]
        self.batches[batch.id] = batch
        self.batch_by_business[batch.business_key] = batch.id
        self.batch_items.update({(item.batch_id, item.item_key): item for item in items})
        return batch

    async def claim_batch(
        self, batch_id: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        # 功能:领取或恢复批任务租约,避免同一批任务被多个执行器处理。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        # 返回:是否成功获得批任务的执行租约。
        batch = self.batches.get(batch_id)
        if batch is None or batch.status not in {"PENDING", "RUNNING"}:
            return False
        if (
            batch.status == "RUNNING"
            and (batch.lease_expires_at or batch.updated_at + timedelta(minutes=5)) > now
        ):
            return False
        for key, item in self.batch_items.items():
            if key[0] == batch_id and item.status in {"RUNNING", "PENDING"}:
                self.batch_items[key] = replace(
                    item,
                    status=("FAILED" if item.status == "RUNNING" else "CANCELLED")
                    if batch.cancel_requested_at
                    else "PENDING",
                    error_code="BATCH_INTERRUPTED_CANCELLED"
                    if batch.cancel_requested_at and item.status == "RUNNING"
                    else None,
                    updated_at=now,
                )
        self.batches[batch_id] = replace(
            batch,
            status="CANCELLED" if batch.cancel_requested_at else "RUNNING",
            updated_at=now,
            lease_token=lease_token or new_ulid(now),
            lease_expires_at=now + timedelta(minutes=5),
            failure_count=sum(
                item.status == "FAILED"
                for (owner, _), item in self.batch_items.items()
                if owner == batch_id
            ),
            completed_at=now if batch.cancel_requested_at else None,
        )
        return True

    async def heartbeat_batch(self, batch_id: str, lease_token: str, now: datetime) -> bool:
        # 功能:核对执行器租约并延长批任务的有效租期。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:租约是否仍归当前执行器持有且已成功续租。
        batch = self.batches[batch_id]
        if batch.lease_token != lease_token or batch.status != "RUNNING":
            return False
        self.batches[batch_id] = replace(batch, lease_expires_at=now + timedelta(minutes=5))
        return True

    async def list_recoverable_batches(self, now: datetime, limit: int = 100) -> list[str]:
        # 功能:查找待执行或租约过期的可恢复批任务。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     limit: 可恢复批任务的最大返回条数。
        # 返回:可恢复批任务的公开标识列表。
        return [
            batch.id
            for batch in self.batches.values()
            if batch.status == "PENDING"
            or (
                batch.status == "RUNNING"
                and (batch.lease_expires_at or batch.updated_at + timedelta(minutes=5)) <= now
            )
        ][:limit]

    async def finish_claimed_batch_item(
        self,
        batch_id: str,
        item_key: str,
        lease_token: str,
        result: dict[str, object],
        error_code: str | None,
        now: datetime,
    ) -> bool:
        # 功能:核对租约后提交任务项结果并汇总批任务执行状态。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     item_key: 批任务内任务项的稳定键,领取和回写结果时据此定位。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        #     result: 本次创建或批任务项执行结果字典,写入幂等回执或审计摘要。
        #     error_code: 执行失败的业务错误代码,成功时通常为空。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:租约是否有效且任务项结果已成功写入。
        batch = self.batches[batch_id]
        item = self.batch_items[(batch_id, item_key)]
        if batch.lease_token != lease_token or item.status != "RUNNING":
            return False
        results = dict(batch.result_payload)
        results[item_key] = {
            "status": "FAILED" if error_code else "SUCCEEDED",
            "error_code": error_code,
            "result": result,
        }
        self.batches[batch_id] = replace(batch, result_payload=results)
        version = result.get("version")
        await MediaAdminService(self).finish_batch_item(
            batch_id,
            item_key,
            succeeded=error_code is None,
            error_code=error_code,
            result_version=version if isinstance(version, int) else None,
            now=now,
        )
        return True

    async def claim_batch_item(
        self, batch_id: str, item_key: str, now: datetime, lease_token: str | None = None
    ) -> bool:
        # 功能:核对批任务租约并领取尚未完成的任务项。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     item_key: 批任务内任务项的稳定键,领取和回写结果时据此定位。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     lease_token: 当前执行器持有的租约令牌,阻止过期执行器更新任务。
        # 返回:是否成功领取该任务项。
        item = self.batch_items.get((batch_id, item_key))
        batch = self.batches.get(batch_id)
        if (
            item is None
            or item.status != "PENDING"
            or batch is None
            or batch.cancel_requested_at
            or (lease_token is not None and batch.lease_token != lease_token)
        ):
            return False
        self.batch_items[(batch_id, item_key)] = replace(
            item, status="RUNNING", attempt_count=item.attempt_count + 1, updated_at=now
        )
        return True

    async def cancel_pending_batch_items(self, batch_id: str, now: datetime) -> None:
        # 功能:将批任务中尚未开始的任务项标记为取消。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        batch = self.batches[batch_id]
        self.batches[batch_id] = replace(batch, cancel_requested_at=now, updated_at=now)
        for key, item in self.batch_items.items():
            if key[0] == batch_id and item.status == "PENDING":
                self.batch_items[key] = replace(item, status="CANCELLED", updated_at=now)

    async def claim_job(self, job_id: str, now: datetime) -> bool:
        # 功能:原子领取待处理的媒体作业并切换为运行状态。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:是否成功将媒体作业从待处理状态切换为运行状态。
        job = await self.get_job(job_id)
        if job is None or job.status != "PENDING":
            return False
        self.jobs[job.business_key] = replace(job, status="RUNNING", updated_at=now)
        return True

    async def get_job_by_business_key(self, business_key: str) -> ProcessingJob | None:
        # 功能:按幂等业务键读取媒体处理作业。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        return self.jobs.get(business_key)

    async def get_job(self, job_id: str) -> ProcessingJob | None:
        # 功能:读取媒体处理作业及供应商调用状态。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:媒体作业及执行状态;未找到对应记录时为 None。
        return next((job for job in self.jobs.values() if job.id == job_id), None)

    async def save_job(self, job: ProcessingJob) -> ProcessingJob:
        # 功能:保存媒体处理作业及其幂等业务键。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job: 媒体处理作业对象,包含输入、状态和供应商请求信息。
        # 返回:媒体作业及执行状态。
        existing = self.jobs.get(job.business_key)
        if existing is not None and existing.id != job.id:
            return existing
        self.jobs[job.business_key] = job
        return job

    async def get_ocr_candidate_by_job(self, job_id: str) -> OcrCandidate | None:
        # 功能:读取指定 OCR 作业对应的识别候选。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:OCR 识别候选及采纳状态;未找到对应记录时为 None。
        return self.ocr_candidates.get(job_id)

    async def save_ocr_candidate(self, candidate: OcrCandidate) -> OcrCandidate:
        # 功能:保存 OCR 文本、结构化识别块及采纳状态。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     candidate: OCR 候选对象,包含原图、文本、结构化块和采纳状态。
        # 返回:OCR 识别候选及采纳状态。
        existing = self.ocr_candidates.get(candidate.job_id)
        if existing is not None and existing.id != candidate.id:
            return existing
        self.ocr_candidates[candidate.job_id] = candidate
        return candidate

    async def get_batch_by_business_key(self, business_key: str) -> BatchJob | None:
        # 功能:按幂等业务键读取批任务。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        batch_id = self.batch_by_business.get(business_key)
        return self.batches.get(batch_id) if batch_id else None

    async def get_batch(self, batch_id: str) -> BatchJob | None:
        # 功能:读取批任务及执行统计。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务及执行统计和当前状态;未找到对应记录时为 None。
        return self.batches.get(batch_id)

    async def list_batches(self) -> list[BatchJob]:
        # 功能:读取批任务列表。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:批任务及执行统计和当前状态的列表。
        # 匿名函数: 按批任务创建时间构造任务列表排序键。
        # 参数:
        #     batch: 待排序的内容批任务对象。
        # 返回: 批任务的创建时间。
        return sorted(self.batches.values(), key=lambda batch: batch.created_at, reverse=True)

    async def save_batch(self, batch: BatchJob) -> BatchJob:
        # 功能:保存批任务状态和执行统计。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch: 批任务对象,包含目标数量、执行统计和状态。
        # 返回:批任务及执行统计和当前状态。
        existing_id = self.batch_by_business.get(batch.business_key)
        if existing_id is not None and existing_id != batch.id:
            return self.batches[existing_id]
        self.batches[batch.id] = batch
        self.batch_by_business[batch.business_key] = batch.id
        return batch

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        # 功能:读取批任务的所有任务项。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务项及执行结果的列表。
        # 匿名函数: 按任务项的稳定键构造批任务项排序键。
        # 参数:
        #     item: 待排序的批任务项对象。
        # 返回: 任务项稳定键字符串。
        return sorted(
            (item for (owner, _), item in self.batch_items.items() if owner == batch_id),
            key=lambda item: item.item_key,
        )

    async def save_batch_item(self, item: BatchJobItem) -> BatchJobItem:
        # 功能:保存单个批任务项的状态、错误和结果版本。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     item: 批任务项对象,包含目标、执行次数和结果状态。
        # 返回:批任务项及执行结果。
        self.batch_items[(item.batch_id, item.item_key)] = item
        return item

    async def get_audio_target(self, target_id: str) -> AudioTarget | None:
        # 功能:读取稳定音频目标及当前生效版本引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:稳定音频目标及生效版本引用;未找到对应记录时为 None。
        return self.audio_targets.get(target_id)

    async def get_audio_target_by_stable_key(self, stable_key: str) -> AudioTarget | None:
        # 功能:按稳定业务键读取音频目标。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        # 返回:稳定音频目标及生效版本引用;未找到对应记录时为 None。
        target_id = self.audio_target_by_stable_key.get(stable_key)
        return self.audio_targets.get(target_id) if target_id else None

    async def save_audio_target(self, target: AudioTarget) -> AudioTarget:
        # 功能:保存音频目标和当前生效版本引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target: 稳定音频目标对象,包含业务键、类型和生效版本引用。
        # 返回:稳定音频目标及生效版本引用。
        existing_id = self.audio_target_by_stable_key.get(target.stable_key)
        if existing_id is not None and existing_id != target.id:
            return self.audio_targets[existing_id]
        self.audio_targets[target.id] = target
        self.audio_target_by_stable_key[target.stable_key] = target.id
        return target

    async def list_audio_targets(self) -> list[AudioTarget]:
        # 功能:读取音频目标列表。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:稳定音频目标及生效版本引用的列表。
        # 匿名函数: 按稳定业务键构造音频目标列表排序键。
        # 参数:
        #     target: 待排序的稳定音频目标对象。
        # 返回: 目标稳定业务键字符串。
        return sorted(self.audio_targets.values(), key=lambda target: target.stable_key)

    async def get_audio_version(self, version_id: str) -> AudioVersion | None:
        # 功能:读取指定音频版本及素材引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     version_id: 音频版本公开标识,定位待确认或回滚的素材版本。
        # 返回:音频版本及其素材、来源和确认状态;未找到对应记录时为 None。
        return self.audio_versions.get(version_id)

    async def find_audio_version_by_provider_request(
        self, provider_request_id: str
    ) -> AudioVersion | None:
        # 功能:按供应商请求标识查找已登记的音频版本。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     provider_request_id: 供应商调用请求标识,用于追踪、轮询或生成结果去重。
        # 返回:音频版本及其素材、来源和确认状态;未找到对应记录时为 None。
        version_id = self.audio_version_by_provider_request.get(provider_request_id)
        return self.audio_versions.get(version_id) if version_id else None

    async def save_audio_version(self, version: AudioVersion) -> AudioVersion:
        # 功能:保存音频版本、来源和生成作业关联。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     version: 音频版本对象,包含素材引用、来源和确认状态。
        # 返回:音频版本及其素材、来源和确认状态。
        if version.provider_request_id:
            existing_id = self.audio_version_by_provider_request.get(version.provider_request_id)
            if existing_id is not None and existing_id != version.id:
                return self.audio_versions[existing_id]
            self.audio_version_by_provider_request[version.provider_request_id] = version.id
        self.audio_versions[version.id] = version
        return version

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        # 功能:读取指定音频目标的全部版本。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:音频版本及其素材、来源和确认状态的列表。
        # 匿名函数: 按版本序号构造音频版本列表排序键。
        # 参数:
        #     version: 待排序的音频版本对象。
        # 返回: 音频的整数版本序号。
        return sorted(
            (version for version in self.audio_versions.values() if version.target_id == target_id),
            key=lambda version: version.version_no,
        )

    async def get_trash_entry(self, entry_id: str) -> TrashEntry | None:
        # 功能:读取草稿回收站记录及保留期限。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        # 返回:草稿回收站记录及保留期限;未找到对应记录时为 None。
        return self.trash_entries.get(entry_id)

    async def get_trash_by_revision(self, revision_id: str) -> TrashEntry | None:
        # 功能:按内容版本读取草稿回收站记录。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:草稿回收站记录及保留期限;未找到对应记录时为 None。
        entry_id = self.trash_by_revision.get(revision_id)
        return self.trash_entries.get(entry_id) if entry_id else None

    async def save_trash_entry(self, entry: TrashEntry) -> TrashEntry:
        # 功能:保存草稿回收站记录和恢复清理时间。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry: 回收站草稿对象,包含场景版本、保留期和恢复清理状态。
        # 返回:草稿回收站记录及保留期限。
        existing_id = self.trash_by_revision.get(entry.revision_id)
        if existing_id is not None and existing_id != entry.id:
            return self.trash_entries[existing_id]
        self.trash_entries[entry.id] = entry
        self.trash_by_revision[entry.revision_id] = entry.id
        return entry

    async def list_trash_entries(self) -> list[TrashEntry]:
        # 功能:读取草稿回收站记录列表。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:草稿回收站记录及保留期限的列表。
        # 匿名函数: 按移入回收站时间构造草稿记录排序键。
        # 参数:
        #     entry: 待排序的草稿回收站记录。
        # 返回: 草稿移入回收站的时间。
        return sorted(self.trash_entries.values(), key=lambda entry: entry.trashed_at, reverse=True)

    async def is_draft_revision(self, scene_id: str, revision_id: str) -> bool:
        # 功能:判断指定内容版本是否为场景可回收的草稿。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:该版本是否是指定场景可回收的草稿。
        return (scene_id, revision_id) in self.drafts

    async def has_draft_references(self, revision_id: str) -> bool:
        # 功能:检查草稿是否被内容包、活动或其他内容版本引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:草稿是否仍被其他业务记录引用。
        return revision_id in self.referenced_drafts

    async def purge_draft(self, scene_id: str, revision_id: str) -> None:
        # 功能:删除草稿并清除场景的草稿版本引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.drafts.discard((scene_id, revision_id))

    async def transition_trash(
        self, entry_id: str, action: str, actor_id: str, now: datetime
    ) -> TrashEntry:
        # 功能:按恢复或清理命令迁移回收站状态,并校验保留期和业务引用。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     action: 业务命令或审计动作代码,标识本次状态迁移或变更类型。
        #     actor_id: 发起操作的管理员公开标识,
        #         写入创建记录、回执或审计。当前实现保留该形参但不参与计算或写入。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:草稿回收站记录及保留期限。
        del actor_id
        entry = self.trash_entries.get(entry_id)
        if entry is None:
            raise AppError("TRASH_ENTRY_NOT_FOUND", "回收站记录不存在", 404)
        if action == "RESTORE" and entry.status == "RESTORED":
            return entry
        if action == "CLEANUP" and entry.status == "CLEANED":
            return entry
        if entry.status != "TRASHED":
            raise AppError("TRASH_ENTRY_NOT_ACTIVE", "回收站记录当前不可操作", 409)
        if (entry.scene_id, entry.revision_id) not in self.drafts:
            raise AppError("DRAFT_NOT_TRASHABLE", "草稿已不存在或已发布", 409)
        if action == "RESTORE":
            result = replace(entry, status="RESTORED", restored_at=now)
        else:
            if now < entry.retention_until:
                raise AppError("TRASH_RETENTION_ACTIVE", "草稿仍在 30 天保留期内", 409)
            if entry.revision_id in self.referenced_drafts:
                raise AppError("DRAFT_REFERENCED", "草稿仍被其他对象引用", 409)
            self.drafts.discard((entry.scene_id, entry.revision_id))
            result = replace(entry, status="CLEANED", cleaned_at=now)
        self.trash_entries[entry_id] = result
        return result

    def register_draft(self, scene_id: str, revision_id: str) -> None:
        # 功能:在内存仓储登记可回收的场景草稿。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.drafts.add((scene_id, revision_id))

    def set_draft_referenced(self, revision_id: str, referenced: bool) -> None:
        # 功能:在内存仓储更新草稿是否被其他业务引用的标记。
        # 参数:
        #     self: 当前 InMemoryMediaAdminRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     referenced: 草稿是否仍被其他业务引用,控制能否彻底清理。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        if referenced:
            self.referenced_drafts.add(revision_id)
        else:
            self.referenced_drafts.discard(revision_id)


class MediaAdminService:
    def __init__(self, repository: MediaAdminRepository) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     repository: 媒体管理仓储,管理作业、批任务、音频版本和草稿回收。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._repository = repository

    async def create_job(
        self,
        *,
        business_key: str,
        job_type: str,
        target_id: str,
        actor_id: str,
        now: datetime,
        batch_id: str | None = None,
        input_payload: dict[str, object] | None = None,
    ) -> ProcessingJob:
        # 功能:按幂等业务键创建或复用媒体处理作业。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        #     job_type: 媒体作业或内容批任务的操作类型代码。
        #     target_id: 媒体处理或音频目标公开标识,定位作业关联对象。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     input_payload: 作业或批任务的原始输入字典,保存模板、文本或批量编辑字段;可为空。
        # 返回:媒体作业及执行状态。
        existing = await self._repository.get_job_by_business_key(business_key)
        if existing is not None:
            return existing
        return await self._repository.save_job(
            ProcessingJob(
                id=new_ulid(now),
                business_key=business_key,
                job_type=job_type,
                target_id=target_id,
                batch_id=batch_id,
                status="PENDING",
                provider_request_id=None,
                error_code=None,
                created_by=actor_id,
                created_at=now,
                updated_at=now,
                input_payload=dict(input_payload or {}),
            )
        )

    async def get_job(self, job_id: str) -> ProcessingJob:
        # 功能:读取媒体处理作业及供应商调用状态。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:媒体作业及执行状态。
        job = await self._repository.get_job(job_id)
        if job is None:
            raise AppError("MEDIA_JOB_NOT_FOUND", "媒体任务不存在", 404)
        return job

    async def save_job_result(
        self,
        job_id: str,
        *,
        status: str,
        provider_request_id: str | None,
        error_code: str | None,
        now: datetime,
    ) -> ProcessingJob:
        # 功能:写入媒体作业状态、供应商请求标识和失败原因。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     status: 待写入或筛选的业务状态代码。
        #     provider_request_id: 供应商调用请求标识,用于追踪、轮询或生成结果去重。
        #     error_code: 执行失败的业务错误代码,成功时通常为空。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:媒体作业及执行状态。
        job = await self.get_job(job_id)
        if job.status in {"SUCCEEDED", "CANCELLED"}:
            return job
        return await self._repository.save_job(
            replace(
                job,
                status=status,
                provider_request_id=provider_request_id,
                error_code=error_code,
                updated_at=now,
            )
        )

    async def cancel_job(self, job_id: str, *, now: datetime) -> ProcessingJob:
        # 功能:取消尚未终止的媒体处理作业。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:媒体作业及执行状态。
        job = await self.get_job(job_id)
        if job.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return job
        return await self._repository.save_job(
            replace(job, status="CANCELLED", cancel_requested_at=now, updated_at=now)
        )

    async def record_ocr_candidate(
        self,
        job_id: str,
        *,
        provider_request_id: str,
        text_value: str,
        blocks: list[dict[str, object]],
        template_type: str,
        now: datetime,
    ) -> OcrCandidate:
        # 功能:将 OCR 供应商结果登记为待确认候选并计算置信度。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     provider_request_id: 供应商调用请求标识,用于追踪、轮询或生成结果去重。
        #     text_value: OCR 供应商识别出的完整文本。
        #     blocks: OCR 识别块列表,含文本、原图位置和可选的数值置信度。
        #     template_type: 内容模板类型,区分 dialogue 对话与 vocabulary 词汇。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:OCR 识别候选及采纳状态。
        existing = await self._repository.get_ocr_candidate_by_job(job_id)
        if existing is not None:
            return existing
        job = await self.get_job(job_id)
        return await self._repository.save_ocr_candidate(
            OcrCandidate(
                id=new_ulid(now),
                job_id=job.id,
                asset_id=job.target_id,
                business_key=job.business_key,
                provider_request_id=provider_request_id,
                status="READY",
                template_type=template_type,
                structured_candidate={"text": text_value, "blocks": blocks},
                confidence=_candidate_confidence(blocks),
                error_code=None,
                created_at=now,
            )
        )

    async def get_ocr_candidate(self, job_id: str) -> OcrCandidate:
        # 功能:读取指定 OCR 作业的识别候选。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        # 返回:OCR 识别候选及采纳状态。
        await self.get_job(job_id)
        candidate = await self._repository.get_ocr_candidate_by_job(job_id)
        if candidate is None:
            raise AppError("OCR_CANDIDATE_NOT_FOUND", "OCR 候选尚未生成", 404)
        return candidate

    async def confirm_ocr_candidate(
        self,
        job_id: str,
        revision_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> OcrCandidate:
        # 功能:将 OCR 候选关联到采纳后的内容草稿版本。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:OCR 识别候选及采纳状态。
        candidate = await self.get_ocr_candidate(job_id)
        if candidate.confirmed_revision_id is not None:
            return candidate
        return await self._repository.save_ocr_candidate(
            replace(
                candidate,
                status="CONFIRMED",
                confirmed_revision_id=revision_id,
                confirmed_by=actor_id,
                confirmed_at=now,
            )
        )

    async def create_batch(
        self,
        *,
        business_key: str,
        job_type: str,
        target_ids: tuple[str, ...],
        actor_id: str,
        now: datetime,
        input_payload: dict[str, object] | None = None,
    ) -> BatchJob:
        # 功能:按业务键创建批任务及去重后的目标任务项。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     business_key: 业务幂等键,重复请求据此复用已有作业或批任务。
        #     job_type: 媒体作业或内容批任务的操作类型代码。
        #     target_ids: 批任务目标公开标识元组,创建时去除重复目标。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     input_payload: 作业或批任务的原始输入字典,保存模板、文本或批量编辑字段;可为空。
        # 返回:批任务及执行统计和当前状态。
        existing = await self._repository.get_batch_by_business_key(business_key)
        if existing is not None:
            return existing
        if not 1 <= len(target_ids) <= 500:
            raise AppError("BATCH_SIZE_INVALID", "批量任务必须包含 1 至 500 项", 422)
        batch = BatchJob(
            id=new_ulid(now),
            business_key=business_key,
            job_type=job_type,
            status="PENDING",
            total_count=len(target_ids),
            success_count=0,
            failure_count=0,
            created_by=actor_id,
            created_at=now,
            updated_at=now,
            input_payload=dict(input_payload or {}),
        )
        items = [
            BatchJobItem(
                id=new_ulid(now),
                batch_id=batch.id,
                item_key=f"{index}:{target_id}",
                target_id=target_id,
                status="PENDING",
                attempt_count=0,
                error_code=None,
                result_version=None,
                updated_at=now,
            )
            for index, target_id in enumerate(target_ids)
        ]
        return await self._repository.create_batch_with_items(batch, items)

    async def get_batch(self, batch_id: str) -> BatchJob:
        # 功能:读取批任务及执行统计。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务及执行统计和当前状态。
        batch = await self._repository.get_batch(batch_id)
        if batch is None:
            raise AppError("BATCH_JOB_NOT_FOUND", "批量任务不存在", 404)
        return batch

    async def list_batches(self) -> list[BatchJob]:
        # 功能:读取批任务列表。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        # 返回:批任务及执行统计和当前状态的列表。
        return await self._repository.list_batches()

    async def list_batch_items(self, batch_id: str) -> list[BatchJobItem]:
        # 功能:读取批任务的所有任务项。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        # 返回:批任务项及执行结果的列表。
        await self.get_batch(batch_id)
        return await self._repository.list_batch_items(batch_id)

    async def finish_batch_item(
        self,
        batch_id: str,
        item_key: str,
        *,
        succeeded: bool,
        error_code: str | None,
        result_version: int | None,
        now: datetime,
    ) -> BatchJobItem:
        # 功能:保存任务项结果并重新汇总批任务完成状态和成功失败数。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     item_key: 批任务内任务项的稳定键,领取和回写结果时据此定位。
        #     succeeded: 该任务项是否执行成功,决定终态和错误字段。
        #     error_code: 执行失败的业务错误代码,成功时通常为空。
        #     result_version: 任务项成功后得到的内容编辑版本号,可为空。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:批任务项及执行结果。
        batch = await self.get_batch(batch_id)
        items = await self._repository.list_batch_items(batch_id)
        item = next((candidate for candidate in items if candidate.item_key == item_key), None)
        if item is None:
            raise AppError("BATCH_ITEM_NOT_FOUND", "批量任务项不存在", 404)
        if item.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return item
        saved = await self._repository.save_batch_item(
            replace(
                item,
                status="SUCCEEDED" if succeeded else "FAILED",
                attempt_count=max(item.attempt_count, 1),
                error_code=None if succeeded else error_code,
                result_version=result_version,
                updated_at=now,
            )
        )
        items = await self._repository.list_batch_items(batch_id)
        success_count = sum(candidate.status == "SUCCEEDED" for candidate in items)
        failure_count = sum(candidate.status == "FAILED" for candidate in items)
        terminal_count = sum(
            candidate.status in {"SUCCEEDED", "FAILED", "CANCELLED"} for candidate in items
        )
        if terminal_count == batch.total_count:
            status = (
                "CANCELLED"
                if batch.cancel_requested_at
                else "COMPLETED"
                if failure_count == 0
                else "COMPLETED_WITH_ERRORS"
            )
            completed_at = now
        else:
            status = "RUNNING"
            completed_at = None
        await self._repository.save_batch(
            replace(
                batch,
                status=status,
                success_count=success_count,
                failure_count=failure_count,
                updated_at=now,
                completed_at=completed_at,
            )
        )
        return saved

    async def cancel_batch(self, batch_id: str, *, now: datetime) -> BatchJob:
        # 功能:请求取消批任务并取消尚未开始的任务项。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     batch_id: 批任务公开标识,关联任务项、执行租约和统计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:批任务及执行统计和当前状态。
        batch = await self.get_batch(batch_id)
        if batch.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED"}:
            return batch
        await self._repository.cancel_pending_batch_items(batch_id, now)
        batch = await self.get_batch(batch_id)
        items = await self._repository.list_batch_items(batch_id)
        saved = replace(
            batch,
            status="RUNNING" if any(item.status == "RUNNING" for item in items) else "CANCELLED",
            success_count=sum(item.status == "SUCCEEDED" for item in items),
            failure_count=sum(item.status == "FAILED" for item in items),
            updated_at=now,
            completed_at=now,
            cancel_requested_at=now,
        )
        return await self._repository.save_batch(saved)

    async def create_audio_candidate(
        self,
        *,
        stable_key: str,
        target_type: str,
        asset_id: str,
        source: str,
        actor_id: str,
        now: datetime,
        provider_request_id: str | None = None,
        processing_job_id: str | None = None,
    ) -> AudioVersion:
        # 功能:按生成请求去重并登记候选音频版本。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;
        #         兼容大写输入。
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        #     source: 音频版本来源代码,区分人工上传与自动生成。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     provider_request_id: 供应商调用请求标识,用于追踪、轮询或生成结果去重。
        #     processing_job_id: 生成音频所关联的媒体作业标识;人工音频可为空。
        # 返回:音频版本及其素材、来源和确认状态。
        if source not in {"MANUAL", "TTS"}:
            raise AppError("AUDIO_SOURCE_INVALID", "音频来源不正确", 422)
        if provider_request_id:
            existing = await self._repository.find_audio_version_by_provider_request(
                provider_request_id
            )
            if existing is not None:
                return existing
        target = await self._repository.get_audio_target_by_stable_key(stable_key)
        if target is None:
            target = await self._repository.save_audio_target(
                AudioTarget(new_ulid(now), stable_key, target_type, None)
            )
        versions = await self._repository.list_audio_versions(target.id)
        version = AudioVersion(
            id=new_ulid(now),
            target_id=target.id,
            asset_id=asset_id,
            version_no=max((candidate.version_no for candidate in versions), default=0) + 1,
            source=source,
            status="CANDIDATE",
            provider_request_id=provider_request_id,
            processing_job_id=processing_job_id,
            created_by=actor_id,
            created_at=now,
        )
        return await self._repository.save_audio_version(version)

    async def create_audio_target(self, stable_key: str, target_type: str) -> AudioTarget:
        # 功能:按稳定业务键和目标类型创建或复用音频目标。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     stable_key: 音频目标的稳定业务键,使不同版本归属于同一目标。
        #     target_type: 音频用途分类,scene 为整段场景,vocabulary 为词汇,chunk 为语块;
        #         兼容大写输入。
        # 返回:稳定音频目标及生效版本引用。
        if target_type not in {"scene", "vocabulary", "chunk", "SCENE", "VOCABULARY", "CHUNK"}:
            raise AppError("AUDIO_TARGET_TYPE_INVALID", "音频目标类型无效", 422)
        existing = await self._repository.get_audio_target_by_stable_key(stable_key)
        if existing is not None:
            if existing.target_type.lower() != target_type.lower():
                raise AppError("AUDIO_TARGET_MISMATCH", "音频目标类型不匹配", 409)
            return existing
        return await self._repository.save_audio_target(
            AudioTarget(new_ulid(datetime.now(UTC)), stable_key, target_type.lower(), None)
        )

    async def get_audio_target(self, target_id: str) -> AudioTarget:
        # 功能:读取稳定音频目标及当前生效版本引用。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:稳定音频目标及生效版本引用。
        target = await self._repository.get_audio_target(target_id)
        if target is None:
            raise AppError("AUDIO_TARGET_NOT_FOUND", "音频目标不存在", 404)
        return target

    async def list_audio_targets(self) -> list[AudioTarget]:
        # 功能:读取音频目标列表。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        # 返回:稳定音频目标及生效版本引用的列表。
        return await self._repository.list_audio_targets()

    async def list_audio_versions(self, target_id: str) -> list[AudioVersion]:
        # 功能:读取指定音频目标的全部版本。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        # 返回:音频版本及其素材、来源和确认状态的列表。
        await self.get_audio_target(target_id)
        return await self._repository.list_audio_versions(target_id)

    async def confirm_audio_version(
        self, version_id: str, *, actor_id: str, now: datetime
    ) -> AudioTarget:
        # 功能:确认候选音频版本并更新目标的生效版本。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     version_id: 音频版本公开标识,定位待确认或回滚的素材版本。
        #     actor_id: 发起操作的管理员公开标识,
        #         写入创建记录、回执或审计。当前实现保留该形参但不参与计算或写入。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:稳定音频目标及生效版本引用。
        del actor_id, now
        version = await self._repository.get_audio_version(version_id)
        if version is None:
            raise AppError("AUDIO_VERSION_NOT_FOUND", "音频版本不存在", 404)
        target = await self.get_audio_target(version.target_id)
        for candidate in await self._repository.list_audio_versions(target.id):
            desired_status = "ACTIVE" if candidate.id == version.id else "SUPERSEDED"
            if candidate.status != desired_status:
                await self._repository.save_audio_version(replace(candidate, status=desired_status))
        target = replace(target, active_version_id=version.id)
        return await self._repository.save_audio_target(target)

    async def rollback_audio_version(
        self,
        target_id: str,
        version_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> AudioTarget:
        # 功能:核对版本归属并将音频目标回滚至指定已确认版本。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     target_id: 音频目标公开标识,关联稳定音频内容及该目标的版本。
        #     version_id: 音频版本公开标识,定位待确认或回滚的素材版本。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:稳定音频目标及生效版本引用。
        target = await self.get_audio_target(target_id)
        version = await self._repository.get_audio_version(version_id)
        if version is None or version.target_id != target.id:
            raise AppError("AUDIO_VERSION_NOT_FOUND", "音频版本不存在", 404)
        return await self.confirm_audio_version(version_id, actor_id=actor_id, now=now)

    async def trash_draft(
        self,
        scene_id: str,
        revision_id: str,
        *,
        actor_id: str,
        now: datetime,
    ) -> TrashEntry:
        # 功能:校验草稿尚未发布后创建三十天保留期的回收站记录。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:草稿回收站记录及保留期限。
        existing = await self._repository.get_trash_by_revision(revision_id)
        if existing is not None:
            return existing
        if not await self._repository.is_draft_revision(scene_id, revision_id):
            raise AppError("DRAFT_NOT_TRASHABLE", "只有未发布草稿可进入回收站", 409)
        return await self._repository.save_trash_entry(
            TrashEntry(
                id=new_ulid(now),
                scene_id=scene_id,
                revision_id=revision_id,
                status="TRASHED",
                trashed_by=actor_id,
                trashed_at=now,
                retention_until=now + timedelta(days=30),
            )
        )

    async def get_trash_entry(self, entry_id: str) -> TrashEntry:
        # 功能:读取草稿回收站记录及保留期限。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        # 返回:草稿回收站记录及保留期限。
        entry = await self._repository.get_trash_entry(entry_id)
        if entry is None:
            raise AppError("TRASH_ENTRY_NOT_FOUND", "回收站记录不存在", 404)
        return entry

    async def list_trash_entries(self) -> list[TrashEntry]:
        # 功能:读取草稿回收站记录列表。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        # 返回:草稿回收站记录及保留期限的列表。
        return await self._repository.list_trash_entries()

    async def restore_draft(self, entry_id: str, *, actor_id: str, now: datetime) -> TrashEntry:
        # 功能:恢复仍在回收站中的场景草稿。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:草稿回收站记录及保留期限。
        return await self._repository.transition_trash(entry_id, "RESTORE", actor_id, now)

    async def cleanup_draft(self, entry_id: str, *, actor_id: str, now: datetime) -> TrashEntry:
        # 功能:在保留期结束且无业务引用时清理回收站草稿。
        # 参数:
        #     self: 当前 MediaAdminService 实例,持有本方法访问的依赖和业务状态。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:草稿回收站记录及保留期限。
        return await self._repository.transition_trash(entry_id, "CLEANUP", actor_id, now)


def _candidate_confidence(blocks: list[dict[str, object]]) -> float | None:
    # 功能:计算 OCR 识别块中数值置信度的算术平均值。
    # 参数:
    #     blocks: OCR 识别块列表,含文本、原图位置和可选的数值置信度。
    # 返回:数值置信度的平均值;没有数值置信度时为 None。
    values = [
        float(value)
        for block in blocks
        if isinstance((value := block.get("confidence")), (int, float))
    ]
    return sum(values) / len(values) if values else None


class MediaService:
    def __init__(
        self,
        oss: OssProvider,
        repository: MediaRepository,
        *,
        signed_url_ttl_seconds: int = 300,
        security: ContentSecurityProvider | None = None,
        ffprobe_path: str = "ffprobe",
        require_review: bool = True,
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     oss: 对象存储供应商,读取、冻结素材字节并生成上传或访问签名。
        #     repository: 素材仓储,保存素材信息、内容摘要和审核状态。
        #     signed_url_ttl_seconds: 素材签名默认有效秒数,实际有效期还受访问权益限制。
        #     security: 内容安全供应商,扫描或查询图片和音频的审核结果。
        #     ffprobe_path: ffprobe 可执行文件路径或命令名,读取音频格式和时长。
        #     require_review: 是否要求素材通过内容审核;关闭时仍保留素材完整性检查。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._oss = oss
        self._repository = repository
        self._signed_url_ttl_seconds = signed_url_ttl_seconds
        self._security = security
        self._ffprobe_path = ffprobe_path
        self._require_review = require_review

    async def get_asset(self, asset_id: str) -> MediaAsset:
        # 功能:读取素材并校验可用状态和不可变对象引用。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        # 返回:素材记录及尺寸、时长和审核状态。
        asset = await self._repository.get(asset_id)
        if asset is None:
            raise AppError("MEDIA_ASSET_NOT_FOUND", "媒体素材不存在", 404)
        if asset.status != "CONFIRMED" or not security_status_usable(
            asset.security_status, require_review=self._require_review
        ):
            raise AppError("MEDIA_ASSET_UNAVAILABLE", "媒体素材未通过检查", 409)
        return await self.ensure_fixed_asset(asset)

    async def ensure_fixed_asset(self, asset: MediaAsset) -> MediaAsset:
        # 功能:为遗留可变对象素材冻结字节并更新素材对象引用。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:素材记录及尺寸、时长和审核状态。
        if asset.object_key.startswith("sealed/media/"):
            return asset
        data = await self.read_asset_bytes(asset)
        fixed = await self._oss.freeze_bytes(data, asset.asset_type, asset.content_type)
        return await self._repository.bind_fixed_object(asset, fixed)

    async def read_asset_bytes(self, asset: MediaAsset) -> bytes:
        # 功能:按素材类型的最大字节限制读取已登记素材内容。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset: 已登记素材对象,包含存储键、审核状态及尺寸或时长。
        # 返回:按上传类型上限读取的素材原始字节。
        data = await self._oss.read_bytes(asset.object_key, self._max_bytes(asset.asset_type))
        inspected = await inspect_media(data, asset.asset_type, self._ffprobe_path)
        if inspected.sha256 != asset.sha256:
            raise AppError("MEDIA_ASSET_CHANGED", "素材字节已变化, 请重新上传确认", 409)
        return data

    async def create_upload_policy(self, asset_type: str, actor_id: str) -> UploadPolicy:
        # 功能:按素材类型和上传人生成受大小限制的上传凭据。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        # 返回:上传地址、对象键前缀、最大字节数和表单凭据。
        max_bytes = self._max_bytes(asset_type)
        prefix = self._prefix(asset_type, actor_id) + new_ulid(datetime.now(UTC)) + "/"
        return await self._oss.create_upload_policy(prefix, max_bytes, 600)

    async def confirm_upload(
        self,
        asset_type: str,
        actor_id: str,
        object_key: str,
        now: datetime,
    ) -> MediaAsset:
        # 功能:校验上传归属,解码并冻结素材,去重后保存审核状态。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:素材记录及尺寸、时长和审核状态。
        prefix = self._prefix(asset_type, actor_id)
        if not object_key.startswith(prefix) or ".." in object_key:
            raise AppError("MEDIA_OBJECT_KEY_INVALID", "素材对象键无效", 422)
        metadata = await self._oss.head_object(object_key)
        self._validate_metadata(asset_type, metadata)
        data = await self._oss.read_bytes(object_key, self._max_bytes(asset_type))
        if not data or len(data) > self._max_bytes(asset_type):
            raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
        inspected = await inspect_media(data, asset_type, self._ffprobe_path)
        existing = await self._repository.get_by_hash(asset_type, inspected.sha256)
        if self._security is None:
            raise AppError("MEDIA_SECURITY_UNAVAILABLE", "未配置独立内容安全检查", 503)
        if existing is not None:
            existing = await self.ensure_fixed_asset(existing)
        if (
            existing is not None
            and existing.status == "CONFIRMED"
            and security_status_usable(
                existing.security_status, require_review=self._require_review
            )
            and (existing.security_request_id or existing.security_status == "SKIPPED")
            and existing.width == inspected.width
            and existing.height == inspected.height
            and existing.duration_ms == inspected.duration_ms
        ):
            return existing
        object_key = (
            existing.object_key
            if existing
            else await self._oss.freeze_bytes(data, asset_type, inspected.content_type)
        )
        if asset_type == "images":
            security = await self._security.scan_image(object_key)
        elif existing is not None and existing.security_request_id:
            security = await self._security.poll_audio(existing.security_request_id)
        else:
            security = await self._security.scan_audio(object_key)
        if existing is not None:
            updated = replace(
                existing,
                content_type=inspected.content_type,
                size=inspected.size,
                width=inspected.width,
                height=inspected.height,
                duration_ms=inspected.duration_ms,
                security_status=security.status,
                status="CONFIRMED"
                if security_status_usable(security.status, require_review=self._require_review)
                else "PENDING",
                security_request_id=security.provider_request_id or None,
            )
            await self._repository.update_security(updated)
            if security.status == "BLOCKED":
                raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
            return updated
        if security.status != "PENDING" and not security_status_usable(
            security.status, require_review=self._require_review
        ):
            raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
        return await self._repository.save(
            MediaAsset(
                new_ulid(now),
                object_key,
                asset_type,
                inspected.content_type,
                inspected.size,
                inspected.sha256,
                "CONFIRMED"
                if security_status_usable(security.status, require_review=self._require_review)
                else "PENDING",
                security.status,
                actor_id,
                now,
                inspected.width,
                inspected.height,
                inspected.duration_ms,
                security.provider_request_id or None,
            )
        )

    def validate_batch(self, asset_type: str, count: int) -> None:
        # 功能:校验单次上传素材的数量不超过对应类型限制。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     count: 单次处理的素材数,图片允许 1 至 30 个,音频允许 1 至 300 个。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        limit = 30 if asset_type == "images" else 300 if asset_type == "audio" else 0
        if count < 1 or count > limit:
            raise AppError("MEDIA_BATCH_LIMIT", "素材批次数量超限", 422)

    async def sign_media(
        self,
        object_key: str,
        entitlement_expires_at: datetime | None,
        now: datetime,
    ) -> SignedMedia:
        # 功能:生成素材访问签名,并将有效期限制在访问权益和实际签名期限内。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     entitlement_expires_at: 访问权益到期时间;为空时不额外缩短默认签名有效期。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:素材访问 URL 及实际到期时间。
        if object_key.startswith(("uploads/images/", "uploads/audio/", "generated/audio/")):
            asset = await self._repository.get_by_object_key(object_key)
            if asset is None:
                raise AppError("MEDIA_ASSET_UNAVAILABLE", "未确认的教学素材不能签名", 409)
            object_key = (await self.get_asset(asset.id)).object_key
        ttl = self._signed_url_ttl_seconds
        if entitlement_expires_at is not None:
            expires_at = (
                entitlement_expires_at
                if entitlement_expires_at.tzinfo
                else entitlement_expires_at.replace(tzinfo=UTC)
            )
            ttl = min(ttl, int((expires_at - now).total_seconds()))
        if ttl <= 0:
            raise AppError("MEDIA_ACCESS_EXPIRED", "媒体访问权限已到期", 403)
        url = await self._oss.sign_get_url(object_key, ttl)
        expires_at = now + timedelta(seconds=ttl)
        query = dict(parse_qsl(urlsplit(url).query))
        # V4 may be shorter than requested when the server uses expiring STS credentials.
        if "x-oss-date" in query and "x-oss-expires" in query:
            try:
                signed_at = datetime.strptime(query["x-oss-date"], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                )
                signature_expiry = signed_at + timedelta(seconds=int(query["x-oss-expires"]))
            except (ValueError, OverflowError):
                raise AppError("OSS_SIGNATURE_INVALID", "OSS签名时间无效", 503) from None
            expires_at = min(expires_at, signature_expiry)
        return SignedMedia(url, expires_at)

    async def sign_feedback_screenshot(
        self,
        object_key: str,
        security_status: str,
        deleted_at: datetime | None,
        now: datetime,
    ) -> SignedMedia:
        # 功能:校验反馈截图未删除且审核可用后生成短期访问签名。
        # 参数:
        #     self: 当前 MediaService 实例,持有本方法访问的依赖和业务状态。
        #     object_key: 对象存储中的完整素材键,定位待读取或签名的字节内容。
        #     security_status: 素材内容审核状态,决定截图是否允许访问。
        #     deleted_at: 反馈截图删除时间;非空时禁止继续生成访问签名。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:素材访问 URL 及实际到期时间。
        if not object_key or ".." in object_key:
            raise AppError("FEEDBACK_SCREENSHOT_INVALID", "反馈截图对象键无效", 422)
        if (
            not security_status_usable(security_status, require_review=self._require_review)
            or deleted_at is not None
        ):
            raise AppError("FEEDBACK_SCREENSHOT_UNAVAILABLE", "反馈截图当前不可访问", 409)
        return await self.sign_media(object_key, None, now)

    @staticmethod
    def _prefix(asset_type: str, actor_id: str) -> str:
        # 功能:按素材类型和上传人构造允许上传的对象键前缀。
        # 参数:
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        # 返回:包含素材类型和管理员标识的上传对象键前缀。
        if asset_type not in {"images", "audio"}:
            raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", actor_id):
            raise AppError("MEDIA_ACTOR_INVALID", "上传者标识无效", 422)
        return f"uploads/{asset_type}/{actor_id}/"

    @staticmethod
    def _max_bytes(asset_type: str) -> int:
        # 功能:返回图片或音频上传允许的最大字节数。
        # 参数:
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        # 返回:对应素材类型允许的最大字节数。
        if asset_type == "images":
            return IMAGE_MAX_BYTES
        if asset_type == "audio":
            return AUDIO_MAX_BYTES
        raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)

    @staticmethod
    def _validate_metadata(asset_type: str, metadata: ObjectMetadata) -> None:
        # 功能:校验上传对象的媒体类型和字节数上限。
        # 参数:
        #     asset_type: 素材分类,图片为 images,音频为 audio。
        #     metadata: 对象存储返回的媒体类型和字节大小元信息。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        allowed_mime = IMAGE_MIME_TYPES if asset_type == "images" else AUDIO_MIME_TYPES
        max_bytes = IMAGE_MAX_BYTES if asset_type == "images" else AUDIO_MAX_BYTES
        if metadata.content_type.lower() not in allowed_mime:
            raise AppError("MEDIA_MIME_INVALID", "素材格式不支持", 422)
        if metadata.size <= 0 or metadata.size > max_bytes:
            raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
        if asset_type == "audio" and not any(
            metadata.object_key.lower().endswith(suffix) for suffix in AUDIO_SUFFIXES
        ):
            raise AppError("MEDIA_EXTENSION_INVALID", "音频扩展名不支持", 422)
