import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from juya_admin_api.integrations.content_security.protocol import SecurityResult
from juya_admin_api.integrations.oss.provider import ObjectMetadata
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 1, tzinfo=UTC)
KEY = "uploads/images/admin-1/a.png"


def png_bytes(width: int = 32, height: int = 24) -> bytes:
    # 功能:按指定像素尺寸生成可解码的 PNG 图片字节。
    # 参数:
    #     width: 生成测试图片的宽度,单位为像素。
    #     height: 生成测试图片的高度,单位为像素。
    # 返回:生成图片的 PNG 编码字节。
    stream = BytesIO()
    Image.new("RGB", (width, height), "blue").save(stream, "PNG")
    return stream.getvalue()


class BytesOss:
    def __init__(self, data: bytes) -> None:
        # 功能:初始化 BytesOss 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 BytesOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     data: 待读取或封存的媒体原始字节。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.data = data

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        # 功能:模拟封存媒体并返回固定命名空间内的测试对象键。
        # 参数:
        #     self: 当前 BytesOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     data: 待读取或封存的媒体原始字节。
        #     asset_type: 媒体资源类别,例如 images 或 audio。
        #     content_type: 媒体 MIME 类型,供上传及封存元数据检查。
        # 返回:固定命名空间内的测试对象键。
        return (
            f"sealed/media/{asset_type}/fixture.png"
            if asset_type == "images"
            else "sealed/media/audio/fixture.wav"
        )

    async def head_object(self, object_key: str) -> ObjectMetadata:
        # 功能:返回测试对象的 MIME、大小和完整性等元数据。
        # 参数:
        #     self: 当前 BytesOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:测试对象元数据。
        return ObjectMetadata(
            object_key,
            len(self.data),
            "image/png",
            "a" * 64,
            {"decodable": "true", "security_status": "PASSED"},
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能:读取预设测试媒体字节,供服务端解码及限量读取检查。
        # 参数:
        #     self: 当前 BytesOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     max_bytes: 允许读取或上传的字节数上限。
        # 返回:预设测试媒体的原始字节。
        assert len(self.data) <= max_bytes
        return self.data


class Security:
    def __init__(self, status: str) -> None:
        # 功能:初始化 Security 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 Security 测试替身实例,保存本用例的预设状态或调用记录。
        #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.status = status
        self.calls = 0

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能:返回预设图片安全审核结果。
        # 参数:
        #     self: 当前 Security 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:预设安全审核结论。
        self.calls += 1
        return SecurityResult("trusted-scan", self.status)


@pytest.mark.asyncio
async def test_confirmation_decodes_server_bytes_and_ignores_client_hash_and_gate() -> None:
    # 功能:验证确认解码服务器字节且忽略客户端哈希及安全结论。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    data = png_bytes()
    security = Security("PASSED")
    service = MediaService(BytesOss(data), InMemoryMediaRepository(), security=security)
    asset = await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert asset.sha256 == hashlib.sha256(data).hexdigest()
    assert (asset.width, asset.height, asset.duration_ms) == (32, 24, None)
    assert security.calls == 1


@pytest.mark.asyncio
async def test_forged_safe_metadata_cannot_confirm_undecodable_or_blocked_file() -> None:
    # 功能:验证伪造安全元数据不能确认无法解码或被拦截文件。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    for data, status, code in [
        (b"not a png", "PASSED", "MEDIA_DECODE_FAILED"),
        (png_bytes(), "BLOCKED", "MEDIA_SECURITY_BLOCKED"),
    ]:
        service = MediaService(BytesOss(data), InMemoryMediaRepository(), security=Security(status))
        with pytest.raises(AppError) as error:
            await service.confirm_upload("images", "admin-1", KEY, NOW)
        assert error.value.code == code


@pytest.mark.asyncio
async def test_unconfigured_security_fails_closed_even_with_valid_image() -> None:
    # 功能:验证安全服务未配置时即使图片有效也拒绝放行。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service = MediaService(BytesOss(png_bytes()), InMemoryMediaRepository())
    with pytest.raises(AppError) as error:
        await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_skipped_review_confirms_real_image_and_reenable_requires_cloud_scan() -> None:
    # 功能:验证跳过审核仍确认真实图片且重启审核需云端检查。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.integrations.content_security.aliyun import (
        SkippedContentSecurityProvider,
    )

    repository = InMemoryMediaRepository()
    oss = BytesOss(png_bytes())
    service = MediaService(
        oss, repository, security=SkippedContentSecurityProvider(), require_review=False
    )
    asset = await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert (asset.status, asset.security_status, asset.security_request_id) == (
        "CONFIRMED",
        "SKIPPED",
        None,
    )
    assert (asset.width, asset.height) == (32, 24)
    assert await service.get_asset(asset.id) == asset
    assert (await service.confirm_upload("images", "admin-1", KEY, NOW)).id == asset.id

    security = Security("PASSED")
    enabled = MediaService(oss, repository, security=security, require_review=True)
    with pytest.raises(AppError) as unavailable:
        await enabled.get_asset(asset.id)
    assert unavailable.value.code == "MEDIA_ASSET_UNAVAILABLE"
    confirmed = await enabled.confirm_upload("images", "admin-1", KEY, NOW)
    assert confirmed.id == asset.id
    assert confirmed.security_status == "PASSED"
    assert security.calls == 1
    assert (await enabled.get_asset(asset.id)).security_request_id == "trusted-scan"


@pytest.mark.asyncio
async def test_review_disabled_still_rejects_invalid_image_bytes() -> None:
    # 功能:验证审核关闭时仍拒绝无效图片字节。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.integrations.content_security.aliyun import (
        SkippedContentSecurityProvider,
    )

    service = MediaService(
        BytesOss(b"not an image"),
        InMemoryMediaRepository(),
        security=SkippedContentSecurityProvider(),
        require_review=False,
    )
    with pytest.raises(AppError) as invalid:
        await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert invalid.value.code == "MEDIA_DECODE_FAILED"


@pytest.mark.asyncio
async def test_quota_reservation_is_atomic_and_idempotent_and_requires_month_verification() -> None:
    # 功能:验证额度预占原子、幂等且要求月份校验。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.media.quota import InMemoryOcrQuotaRepository, OcrQuotaService

    quota = OcrQuotaService(InMemoryOcrQuotaRepository())
    with pytest.raises(AppError) as disabled:
        await quota.reserve("job-1", NOW)
    assert disabled.value.code == "OCR_DISABLED"
    await quota.configure(
        enabled=True,
        monthly_limit=1,
        free_quota=1,
        paid_disabled=True,
        verify_quota=True,
        actor_id="admin-1",
        now=NOW,
    )
    outcomes = await asyncio.gather(
        quota.reserve("job-1", NOW), quota.reserve("job-2", NOW), return_exceptions=True
    )
    assert sum(isinstance(item, AppError) for item in outcomes) == 1
    winner = "job-1" if not isinstance(outcomes[0], AppError) else "job-2"
    await quota.reserve(winner, NOW)
    assert (await quota.status(NOW))["reserved_count"] == 1
    with pytest.raises(AppError) as expired:
        await quota.reserve("job-next", datetime(2026, 11, 1, tzinfo=UTC))
    assert expired.value.code == "OCR_QUOTA_UNVERIFIED"


@pytest.mark.asyncio
async def test_pending_audio_confirmation_reuses_security_task_and_blocks_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证待审核音频确认复用安全任务且禁止访问。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.media.inspection import InspectedMedia

    class AudioOss(BytesOss):
        async def head_object(self, object_key: str) -> ObjectMetadata:
            # 功能:返回测试对象的 MIME、大小和完整性等元数据。
            # 参数:
            #     self: 当前 AudioOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            # 返回:测试对象元数据。
            return ObjectMetadata(object_key, 16, "audio/wav", "", {})

    class AsyncSecurity:
        submitted = 0
        queried = 0

        async def scan_audio(self, object_key: str) -> SecurityResult:
            # 功能:返回预设音频安全审核请求结果。
            # 参数:
            #     self: 当前 AsyncSecurity 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            # 返回:预设安全审核结论。
            self.submitted += 1
            return SecurityResult("audio-task-1", "PENDING")

        async def poll_audio(self, request_id: str) -> SecurityResult:
            # 功能:返回预设音频审核任务查询结果。
            # 参数:
            #     self: 当前 AsyncSecurity 测试替身实例,保存本用例的预设状态或调用记录。
            #     request_id: 注销清理请求或云端审核请求标识。
            # 返回:预设安全审核结论。
            assert request_id == "audio-task-1"
            self.queried += 1
            return SecurityResult(request_id, "PASSED")

    async def inspect(data: bytes, kind: str, path: str) -> InspectedMedia:
        # 功能:返回模拟音频检查事实,隔离真实媒体探测进程。
        # 参数:
        #     data: 待读取或封存的媒体原始字节。
        #     kind: 测试选择的操作或媒体类别,决定所执行的模拟分支。
        #     path: 媒体探测工具的可执行文件路径。
        # 返回:媒体检查事实。
        return InspectedMedia("audio/wav", 16, "f" * 64, duration_ms=1000)

    monkeypatch.setattr("juya_admin_api.modules.media.service.inspect_media", inspect)
    security = AsyncSecurity()
    service = MediaService(AudioOss(b"fixture"), InMemoryMediaRepository(), security=security)
    key = "uploads/audio/admin-1/fixtures/a.wav"
    pending = await service.confirm_upload("audio", "admin-1", key, NOW)
    assert pending.status == pending.security_status == "PENDING"
    with pytest.raises(AppError):
        await service.get_asset(pending.id)
    confirmed = await service.confirm_upload("audio", "admin-1", key, NOW)
    assert confirmed.id == pending.id and confirmed.status == "CONFIRMED"
    assert (security.submitted, security.queried) == (1, 1)


@pytest.mark.asyncio
async def test_legacy_unbound_target_signature_fails_closed() -> None:
    # 功能:验证旧版未绑定目标的签名请求拒绝放行。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.media.repository import SQLAlchemySignedTargetResolver

    resolver = SQLAlchemySignedTargetResolver(None, None)
    with pytest.raises(AppError) as error:
        await resolver("unbound-target", "user", NOW)
    assert error.value.code == "SCENE_RESOURCE_BINDING_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("frames,stderr", [("0", b""), ("1", b"invalid audio frame")])
async def test_audio_inspection_requires_decoded_frames_and_no_decoder_errors(
    monkeypatch: pytest.MonkeyPatch, frames: str, stderr: bytes
) -> None:
    # 功能:验证音频检查要求已解码帧且无解码器错误。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     frames: ffprobe 替身返回的已解码帧数。
    #     stderr: ffprobe 替身返回的错误输出字节。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.media.inspection import inspect_media

    def probe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # 功能:检查 ffprobe 解码选项并返回预设帧数和错误输出。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:subprocess.CompletedProcess[bytes],由本用例预设的数据或所组装的测试资源构成。
        assert "-count_frames" in args
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "format": {"duration": "1.000", "format_name": "wav"},
                    "streams": [{"codec_type": "audio", "nb_read_frames": frames}],
                }
            ).encode(),
            stderr,
        )

    monkeypatch.setattr("juya_admin_api.modules.media.inspection.subprocess.run", probe)
    with pytest.raises(AppError) as error:
        await inspect_media(b"header without valid frames", "audio")
    assert error.value.code == "MEDIA_DECODE_FAILED"
