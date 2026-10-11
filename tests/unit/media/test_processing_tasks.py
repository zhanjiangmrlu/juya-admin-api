from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.ocr.protocol import OcrResult
from juya_admin_api.integrations.tts.protocol import TtsResult
from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService
from juya_admin_api.modules.media.tasks import (
    InMemoryMediaJobRepository,
    MediaTaskService,
    PersistentMediaTaskService,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class FailingOcr:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能:模拟 OCR 服务,返回预设结果或注入识别失败。
        # 参数:
        #     self: 当前 FailingOcr 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OCR 待识别图片的 OSS 对象键。
        #     template_type: OCR 模板类型,决定识别结果按对话或词汇等内容组织。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise RuntimeError(f"provider unavailable: {object_key}:{template_type}")


class FakeTts:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        # 功能:模拟 TTS 服务,返回预设音频结果或注入合成失败。
        # 参数:
        #     self: 当前 FakeTts 测试替身实例,保存本用例的预设状态或调用记录。
        #     audio_target: 待合成音频的业务目标标识。
        #     voice: TTS 合成使用的音色标识。
        #     text: TTS 待朗读文本或安全检查的文本内容。
        # 返回:预设 TTS 合成结果。
        del voice, text
        return TtsResult("provider-1", f"generated/{audio_target}.mp3", 1200)


class CountingOcr:
    def __init__(self) -> None:
        # 功能:初始化 CountingOcr 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 CountingOcr 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.calls = 0

    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能:模拟 OCR 服务,返回预设结果或注入识别失败。
        # 参数:
        #     self: 当前 CountingOcr 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OCR 待识别图片的 OSS 对象键。
        #     template_type: OCR 模板类型,决定识别结果按对话或词汇等内容组织。
        # 返回:预设 OCR 识别结果。
        self.calls += 1
        return OcrResult(
            f"ocr-provider-{self.calls}",
            f"recognized:{object_key}:{template_type}",
            [{"type": "title", "text": "Coffee time", "confidence": 0.98}],
        )


class CountingTts:
    def __init__(self) -> None:
        # 功能:初始化 CountingTts 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 CountingTts 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.calls = 0

    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        # 功能:模拟 TTS 服务,返回预设音频结果或注入合成失败。
        # 参数:
        #     self: 当前 CountingTts 测试替身实例,保存本用例的预设状态或调用记录。
        #     audio_target: 待合成音频的业务目标标识。
        #     voice: TTS 合成使用的音色标识。
        #     text: TTS 待朗读文本或安全检查的文本内容。
        # 返回:预设 TTS 合成结果。
        self.calls += 1
        return TtsResult(
            f"tts-provider-{self.calls}",
            f"generated/audio/{audio_target}/{voice}/{self.calls}.mp3",
            len(text) * 100,
        )


@pytest.mark.asyncio
async def test_batch_items_fail_independently_and_job_key_is_idempotent() -> None:
    # 功能:验证批次条目独立失败且任务业务键幂等。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryMediaJobRepository()
    service = MediaTaskService(repository, FailingOcr(), FakeTts())

    first = await service.run_tts_batch(
        "tts:batch-1",
        (
            ("target-1", "hello", "voice-a"),
            ("target-2", "", "voice-a"),
            ("target-3", "world", "voice-a"),
        ),
        NOW,
    )
    replay = await service.run_tts_batch("tts:batch-1", (("target-4", "ignored", "voice-a"),), NOW)

    assert first is replay
    assert (first.total_count, first.success_count, first.failure_count) == (3, 0, 3)
    assert repository.audio_targets == {}


@pytest.mark.asyncio
async def test_tts_redelivery_is_disabled_without_replacing_manual_active() -> None:
    # 功能:验证禁止的 TTS 重投不替换人工激活版本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    tts = CountingTts()
    manual = await admin.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="manual-asset",
        source="MANUAL",
        actor_id="admin-1",
        now=NOW,
    )
    target = await admin.confirm_audio_version(manual.id, actor_id="admin-1", now=NOW)
    job = await admin.create_job(
        business_key="tts:sentence-1",
        job_type="TTS",
        target_id=target.id,
        actor_id="admin-1",
        now=NOW,
    )
    # 匿名函数: 从合成结果提取对象键并注册稳定测试资源。
    # 参数:
    #     result: TTS 合成结果, 从 object_key 提取生成音频对象。
    #     now: 注册资源时使用的测试时间。
    # 返回: 资源注册回调产生的可等待对象, 等待后得到测试资源标识。
    worker = PersistentMediaTaskService(
        admin,
        repository,
        CountingOcr(),
        tts,
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )

    await worker.run_tts(
        job.id,
        stable_key="sentence-1",
        target_type="SENTENCE",
        text="Hello",
        voice="en-US-1",
        now=NOW,
    )
    await worker.run_tts(
        job.id,
        stable_key="sentence-1",
        target_type="SENTENCE",
        text="Hello",
        voice="en-US-1",
        now=NOW,
    )

    refreshed = await admin.get_audio_target(target.id)
    versions = await admin.list_audio_versions(target.id)
    assert refreshed.active_version_id == manual.id
    assert tts.calls == 0
    assert len(versions) == 1
    assert versions[-1].source == "MANUAL"
    assert versions[-1].status == "ACTIVE"


async def _asset_id(object_key: str, now: datetime) -> str:
    # 功能:根据生成对象键构造稳定测试资源标识。
    # 参数:
    #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
    #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
    # 返回:字符串。
    del now
    return f"asset:{object_key}"


@pytest.mark.asyncio
async def test_tts_registration_failure_is_terminal_and_sanitized() -> None:

    # 功能:验证 TTS 注册失败为终态且错误脱敏。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    async def fail(result: TtsResult, now: datetime) -> str:
        # 功能:模拟注册生成音频失败且错误包含需要脱敏的签名 URL。
        # 参数:
        #     result: 预设业务结果或生成音频结果,供模拟完成记录或故障注入。
        #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise RuntimeError("https://private.test/?x-oss-signature=private-secret")

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    worker = PersistentMediaTaskService(
        admin, repository, CountingOcr(), CountingTts(), register_generated_audio=fail
    )
    job = await admin.create_job(
        business_key="tts:fail", job_type="TTS", target_id="target", actor_id="admin-1", now=NOW
    )
    result = await worker.run_tts(
        job.id, stable_key="sentence", target_type="SENTENCE", text="Hello", voice="en", now=NOW
    )
    assert result.status == "FAILED"
    assert result.error_code == "TTS_DISABLED"
    assert "private-secret" not in repr(result)


@pytest.mark.asyncio
async def test_tts_output_outside_generated_audio_is_rejected() -> None:
    # 功能:验证生成音频目录外的 TTS 输出被拒绝。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    # 匿名函数: 从合成结果提取对象键并注册稳定测试资源。
    # 参数:
    #     result: TTS 合成结果, 从 object_key 提取生成音频对象。
    #     now: 注册资源时使用的测试时间。
    # 返回: 资源注册回调产生的可等待对象, 等待后得到测试资源标识。
    worker = PersistentMediaTaskService(
        admin,
        repository,
        CountingOcr(),
        FakeTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="tts:bad-key", job_type="TTS", target_id="target", actor_id="admin-1", now=NOW
    )
    result = await worker.run_tts(
        job.id, stable_key="sentence", target_type="SENTENCE", text="Hello", voice="en", now=NOW
    )
    assert result.status == "FAILED"
    assert result.error_code == "TTS_DISABLED"
    assert repository.audio_versions == {}
