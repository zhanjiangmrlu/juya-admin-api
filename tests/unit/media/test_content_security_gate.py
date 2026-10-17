import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from alibabacloud_green20220302 import models  # type: ignore[import-untyped]

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.content_security.aliyun import (
    AliyunContentSecurityProvider,
    LocalFixtureContentSecurityProvider,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "risk", "expected_status"),
    [
        (200, "none", "PASSED"),
        (200, "low", "BLOCKED"),
        (200, "medium", "BLOCKED"),
        (200, "high", "BLOCKED"),
        (200, "unknown", "BLOCKED"),
        (200, None, None),
        (400, "none", None),
        (408, "none", None),
    ],
)
async def test_text_plus_uses_valid_service_and_requires_completed_safe_result(
    code: int, risk: str | None, expected_status: str | None
) -> None:
    # 功能:验证文本审核 Plus SDK 请求契约,只接受完成且明确安全的结论
    # 参数 code 为云端返回码,risk 为风险等级,expected_status 为允许的状态或不可用预期
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    response = {"Code": code, "RequestId": "text-scan-1", "Data": {"RiskLevel": risk}}
    call = Mock(
        return_value=SimpleNamespace(body=SimpleNamespace(to_map=Mock(return_value=response)))
    )
    provider.client = SimpleNamespace(text_moderation_plus_with_options=call)
    content = "Hello, friend. 你好。"

    if expected_status is None:
        with pytest.raises(AppError) as error:
            await provider.scan_text(content)
        assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"
    else:
        result = await provider.scan_text(content)
        assert result.status == expected_status
        assert result.provider_request_id == "text-scan-1"

    call.assert_called_once()
    request, options = call.call_args.args
    assert isinstance(request, models.TextModerationPlusRequest)
    assert request.service == "ugc_moderation_byllm"
    assert json.loads(request.service_parameters) == {"content": content}
    assert options.autoretry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_data", [{"RiskLevel": "none"}, {"RiskLevel": "low"}, None])
async def test_audio_poll_stays_pending_until_code_200(pending_data: dict[str, str] | None) -> None:
    # 功能:验证检测中的临时风险等级不能终结审核,下一次完成结果才可通过
    # 参数 pending_data 为检测中返回的临时风险信息或缺失的 Data
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    pending_body: dict[str, Any] = {"Code": 280, "RequestId": "poll-pending"}
    if pending_data is not None:
        pending_body["Data"] = pending_data
    complete_body = {"Code": 200, "RequestId": "poll-complete", "Data": {"RiskLevel": "none"}}
    call = Mock(
        side_effect=[
            SimpleNamespace(body=SimpleNamespace(to_map=Mock(return_value=body)))
            for body in [pending_body, complete_body]
        ]
    )
    provider.client = SimpleNamespace(voice_moderation_result_with_options=call)

    pending = await provider.poll_audio("original-audio-task")
    assert pending.status == "PENDING"
    assert pending.provider_request_id == "original-audio-task"
    completed = await provider.poll_audio("original-audio-task")
    assert completed.status == "PASSED"
    assert completed.provider_request_id == "original-audio-task"
    assert call.call_count == 2
    for args in call.call_args_list:
        request, options = args.args
        assert isinstance(request, models.VoiceModerationResultRequest)
        assert request.service == "audio_media_detection"
        assert json.loads(request.service_parameters) == {"taskId": "original-audio-task"}
        assert options.autoretry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 408])
async def test_audio_poll_rejects_provider_errors_even_with_safe_risk(code: int) -> None:
    # 功能:验证轮询错误码不能因临时安全等级变成完成或待审核结果
    # 参数 code 为参数错误或未授权的云端错误码
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    response = {"Code": code, "RequestId": "poll-error", "Data": {"RiskLevel": "none"}}
    call = Mock(
        return_value=SimpleNamespace(body=SimpleNamespace(to_map=Mock(return_value=response)))
    )
    provider.client = SimpleNamespace(voice_moderation_result_with_options=call)
    with pytest.raises(AppError) as error:
        await provider.poll_audio("original-audio-task")
    assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"
    call.assert_called_once()


@pytest.mark.asyncio
async def test_code_280_is_not_accepted_by_text_image_or_audio_submission() -> None:
    # 功能:验证检测中状态仅适用于音频轮询,不放宽其它审核接口的成功契约
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    response = {
        "Code": 280,
        "RequestId": "not-complete",
        "Data": {"RiskLevel": "none", "TaskId": "unexpected-task"},
    }
    call = Mock(
        return_value=SimpleNamespace(body=SimpleNamespace(to_map=Mock(return_value=response)))
    )
    provider.client = SimpleNamespace(
        text_moderation_plus_with_options=call,
        image_moderation_with_options=call,
        voice_moderation_with_options=call,
    )
    for method, value in [
        (provider.scan_text, "Hello"),
        (provider.scan_image, "uploads/images/admin/photo.png"),
        (provider.scan_audio, "uploads/audio/admin/lesson.wav"),
    ]:
        with pytest.raises(AppError) as error:
            await method(value)
        assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"
    assert call.call_count == 3
    assert all(args.args[1].autoretry is False for args in call.call_args_list)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "test"])
async def test_local_fixture_poll_accepts_only_its_own_submitted_identity(environment: str) -> None:
    provider = LocalFixtureContentSecurityProvider(environment, explicitly_enabled=True)
    submitted = await provider.scan_audio("sealed/media/audio/fixtures/synthetic.wav")
    assert submitted.provider_request_id == "local-synthetic-fixture"
    result = await provider.poll_audio(submitted.provider_request_id)
    assert result.status == "PASSED" and result.provider_request_id == submitted.provider_request_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_id",
    ["real-green-pending-task", "local-synthetic-fixture-suffix", "", " local-synthetic-fixture "],
)
async def test_local_fixture_poll_rejects_external_or_nonexact_task_identity(
    request_id: str,
) -> None:
    provider = LocalFixtureContentSecurityProvider("test", explicitly_enabled=True)
    with pytest.raises(AppError) as error:
        await provider.poll_audio(request_id)
    assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"
