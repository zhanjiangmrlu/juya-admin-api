from datetime import UTC, datetime

import pytest
from test_v13_media import Security, png_bytes

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class FakeOss:
    def __init__(self) -> None:
        # 功能:初始化 FakeOss 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.metadata: dict[str, ObjectMetadata] = {}

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        # 功能:模拟生成限定前缀、大小和有效期的 OSS 上传策略。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key_prefix: 上传策略授权的对象键前缀,限制可写目录。
        #     max_bytes: 允许读取或上传的字节数上限。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:模拟上传策略。
        return UploadPolicy("https://upload.example", object_key_prefix, max_bytes, expires_in, {})

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        # 功能:模拟封存媒体并返回固定命名空间内的测试对象键。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
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
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:测试对象元数据。
        return self.metadata[object_key]

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能:读取预设测试媒体字节,供服务端解码及限量读取检查。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     max_bytes: 允许读取或上传的字节数上限。
        # 返回:预设测试媒体的原始字节。
        return b"invalid" if object_key.endswith("decode.png") else png_bytes()

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        return f"https://oss.example/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        # 功能:模拟删除指定对象并记录清理操作。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.metadata.pop(object_key, None)


@pytest.mark.asyncio
async def test_upload_policy_and_confirmation_validate_prefix_metadata_and_deduplicate() -> None:
    # 功能:验证上传策略及确认检查前缀、元数据并去重。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    oss = FakeOss()
    repository = InMemoryMediaRepository()
    service = MediaService(oss, repository, security=Security("PASSED"))
    object_key = "uploads/images/admin-1/image.png"
    oss.metadata[object_key] = ObjectMetadata(
        object_key,
        1024,
        "image/png",
        "a" * 64,
        {"decodable": "true", "security_status": "PASSED"},
    )

    policy = await service.create_upload_policy("images", "admin-1")
    first = await service.confirm_upload("images", "admin-1", object_key, NOW)
    duplicate = await service.confirm_upload("images", "admin-1", object_key, NOW)

    assert policy.object_key_prefix.startswith("uploads/images/admin-1/")
    assert policy.object_key_prefix != "uploads/images/admin-1/"
    assert first.id == duplicate.id
    assert len(repository.assets) == 1

    with pytest.raises(AppError) as wrong_prefix:
        await service.confirm_upload("images", "admin-1", "uploads/images/other/x.png", NOW)
    assert wrong_prefix.value.code == "MEDIA_OBJECT_KEY_INVALID"


@pytest.mark.asyncio
async def test_confirmation_rejects_type_size_decode_security_and_batch_limits() -> None:
    # 功能:验证确认拒绝无效类型、大小、解码、安全结论及批次额度。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    oss = FakeOss()
    service = MediaService(oss, InMemoryMediaRepository(), security=Security("BLOCKED"))
    prefix = "uploads/images/admin-1/"
    cases = {
        "mime.png": ObjectMetadata(prefix + "mime.png", 10, "text/plain", "1" * 64, {}),
        "large.png": ObjectMetadata(
            prefix + "large.png", 21 * 1024 * 1024, "image/png", "2" * 64, {}
        ),
        "decode.png": ObjectMetadata(
            prefix + "decode.png", 10, "image/png", "3" * 64, {"decodable": "false"}
        ),
        "blocked.png": ObjectMetadata(
            prefix + "blocked.png",
            10,
            "image/png",
            "4" * 64,
            {"decodable": "true", "security_status": "BLOCKED"},
        ),
    }
    for name, metadata in cases.items():
        oss.metadata[metadata.object_key] = metadata
        with pytest.raises(AppError):
            await service.confirm_upload("images", "admin-1", prefix + name, NOW)

    with pytest.raises(AppError) as image_batch:
        service.validate_batch("images", 31)
    assert image_batch.value.code == "MEDIA_BATCH_LIMIT"
    service.validate_batch("audio", 300)
    with pytest.raises(AppError):
        service.validate_batch("audio", 301)
