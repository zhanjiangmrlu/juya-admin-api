from types import SimpleNamespace
from typing import Any

import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.content_security.aliyun import (
    AliyunContentSecurityProvider,
    create_content_security_provider,
)
from juya_admin_api.shared.errors import AppError


class SignedOss:
    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 SignedOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        return "https://private-oss.test/image?signature=fixture"


@pytest.mark.asyncio
async def test_aliyun_gate_requires_explicit_safe_provider_conclusion() -> None:
    # 功能:验证阿里云审核要求明确安全结论。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    for risk in ["none", "low", "medium", "high", None]:

        def call(request: Any, options: Any, risk: str | None = risk) -> Any:
            # 功能:检查审核 SDK 的服务名及重试选项并返回预设风险等级。
            # 参数:
            #     request: 被测试适配器提交的 SDK 请求对象,供检查桶、对象或审核服务参数。
            #     options: 传给阿里云审核 SDK 的运行选项,用于检查自动重试设置。
            #     risk: 云端审核替身返回的风险等级。
            # 返回:Any,由本用例预设的数据或所组装的测试资源构成。
            assert options.autoretry is False
            assert request.service == "baselineCheck"
            # 匿名函数: 序列化预设云端安全审核响应, 覆盖不同风险结论。
            # 参数: 无。
            # 返回: 包含成功代码、审核请求标识及预设风险等级的字典。
            return SimpleNamespace(
                body=SimpleNamespace(
                    to_map=lambda: {"Code": 200, "RequestId": "scan-1", "Data": {"RiskLevel": risk}}
                )
            )

        provider.client = SimpleNamespace(image_moderation_with_options=call)
        if risk is None:
            with pytest.raises(AppError):
                await provider.scan_image("uploads/images/1/a.png")
        else:
            result = await provider.scan_image("uploads/images/1/a.png")
            assert result.status == ("PASSED" if risk == "none" else "BLOCKED")


def test_local_security_cannot_activate_in_production() -> None:
    # 功能:验证本地安全服务不能在生产环境启用。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    with pytest.raises(RuntimeError):
        create_content_security_provider(
            Settings(
                environment="production",
                content_security_enabled=True,
                content_security_provider="local",
                content_security_local_fixtures_only=True,
            ),
            SignedOss(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "test", "production"])
async def test_disabled_review_reports_skipped_without_cloud_credentials(environment: str) -> None:
    # 功能:验证关闭审核时返回跳过状态且不需要云凭证。
    # 参数:
    #     environment: 参数化测试选择的运行环境,例如 local、test 或 production。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    provider = create_content_security_provider(
        Settings(environment=environment, content_security_provider="aliyun"), SignedOss()
    )
    for result in [
        await provider.scan_image("uploads/images/admin/photo.png"),
        await provider.scan_audio("uploads/audio/admin/lesson.wav"),
        await provider.scan_text("lesson"),
        await provider.poll_audio("previous-task"),
    ]:
        assert result.status == "SKIPPED"
        assert result.provider_request_id == ""
