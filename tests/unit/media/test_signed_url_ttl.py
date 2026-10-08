from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class SigningOss:
    def __init__(self) -> None:
        # 功能:初始化 SigningOss 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 SigningOss 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.ttls: list[int] = []

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        # 功能:模拟生成限定前缀、大小和有效期的 OSS 上传策略。
        # 参数:
        #     self: 当前 SigningOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key_prefix: 上传策略授权的对象键前缀,限制可写目录。
        #     max_bytes: 允许读取或上传的字节数上限。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise NotImplementedError

    async def head_object(self, object_key: str) -> ObjectMetadata:
        # 功能:返回测试对象的 MIME、大小和完整性等元数据。
        # 参数:
        #     self: 当前 SigningOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise NotImplementedError

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 SigningOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        self.ttls.append(expires_in)
        return f"https://oss.example/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        # 功能:模拟删除指定对象并记录清理操作。
        # 参数:
        #     self: 当前 SigningOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise NotImplementedError


@pytest.mark.asyncio
async def test_signed_url_ttl_is_truncated_to_entitlement_expiry() -> None:
    # 功能:验证签名 URL 时长截断至权益到期时间。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    oss = SigningOss()
    service = MediaService(oss, InMemoryMediaRepository(), signed_url_ttl_seconds=300)

    signed = await service.sign_media("audio/target-1.mp3", NOW + timedelta(seconds=91), NOW)

    assert signed.expires_at == NOW + timedelta(seconds=91)
    assert oss.ttls == [91]


@pytest.mark.asyncio
async def test_signed_url_rejects_zero_or_expired_ttl() -> None:
    # 功能:验证零值或过期签名时长被拒绝。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    oss = SigningOss()
    service = MediaService(oss, InMemoryMediaRepository())

    with pytest.raises(AppError) as expired:
        await service.sign_media("audio/target-1.mp3", NOW, NOW)
    assert expired.value.code == "MEDIA_ACCESS_EXPIRED"
    assert oss.ttls == []


@pytest.mark.asyncio
async def test_reported_expiry_does_not_outlive_sts_capped_oss_signature() -> None:
    # 功能:验证报告到期时间不超过 STS 限制后的 OSS 签名。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    class StsSigningOss(SigningOss):
        async def sign_get_url(self, object_key: str, expires_in: int) -> str:
            # 功能:模拟对象下载签名并保留有效期供断言。
            # 参数:
            #     self: 当前 StsSigningOss 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
            # 返回:预设资源签名 URL 字符串。
            return "https://oss.example/a?x-oss-date=20260929T000000Z&x-oss-expires=60&x-oss-signature=private"

    service = MediaService(StsSigningOss(), InMemoryMediaRepository())
    signed = await service.sign_media("feedback/user/a.png", None, NOW)
    assert signed.expires_at == NOW + timedelta(seconds=60)
    assert "private" not in repr(signed)
