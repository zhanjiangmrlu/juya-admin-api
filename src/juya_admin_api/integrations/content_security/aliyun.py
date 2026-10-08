import asyncio
import json
from typing import Any

from alibabacloud_green20220302 import models  # type: ignore[import-untyped]
from alibabacloud_green20220302.client import Client  # type: ignore[import-untyped]
from alibabacloud_tea_openapi.models import Config  # type: ignore[import-untyped]
from alibabacloud_tea_util.models import RuntimeOptions  # type: ignore[import-untyped]

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.content_security.protocol import (
    ContentSecurityProvider,
    SecurityResult,
)
from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.shared.errors import AppError


class SkippedContentSecurityProvider:
    async def scan_text(self, text: str) -> SecurityResult:
        # 功能: 在审核关闭时返回 SKIPPED,避免发起云审核请求.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 审核关闭的 SKIPPED 结果,不发起云服务请求.
        return SecurityResult("", "SKIPPED")

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能: 在审核关闭时返回 SKIPPED,避免发起云审核请求.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核关闭的 SKIPPED 结果,不发起云服务请求.
        return await self.scan_text(object_key)

    async def scan_audio(self, object_key: str) -> SecurityResult:
        # 功能: 在审核关闭时返回 SKIPPED,避免发起云审核请求.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核关闭的 SKIPPED 结果,不发起云服务请求.
        return await self.scan_text(object_key)

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        # 功能: 在审核关闭时返回 SKIPPED,避免发起云审核请求.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     provider_request_id: 云内容安全提供方返回的音频审核任务标识.
        # 返回: 审核关闭的 SKIPPED 结果,不发起云服务请求.
        return await self.scan_text(provider_request_id)


class DisabledContentSecurityProvider:
    async def scan_text(self, text: str) -> SecurityResult:
        # 功能: 在审核提供方未配置时返回明确的服务不可用错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 不正常返回;抛出审核服务不可用的业务异常.
        raise AppError("MEDIA_SECURITY_UNAVAILABLE", "未配置独立阿里云内容安全提供方", 503)

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能: 在审核提供方未配置时返回明确的服务不可用错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 不正常返回;抛出审核服务不可用的业务异常.
        return await self.scan_text(object_key)

    async def scan_audio(self, object_key: str) -> SecurityResult:
        # 功能: 在审核提供方未配置时返回明确的服务不可用错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 不正常返回;抛出审核服务不可用的业务异常.
        return await self.scan_text(object_key)

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        # 功能: 在审核提供方未配置时返回明确的服务不可用错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     provider_request_id: 云内容安全提供方返回的音频审核任务标识.
        # 返回: 不正常返回;抛出审核服务不可用的业务异常.
        return await self.scan_text(provider_request_id)


class LocalFixtureContentSecurityProvider:
    def __init__(self, environment: str, *, explicitly_enabled: bool) -> None:
        # 功能: 校验当前环境及明确启用标记,仅允许本地或测试合成审核.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     environment: 当前部署环境名称,仅 local 或 test 可启用本地合成素材提供方.
        #     explicitly_enabled: 是否明确允许在本地或测试环境使用合成审核结果.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if environment not in {"local", "test"} or not explicitly_enabled:
            raise RuntimeError("local content security requires explicit local/test fixture opt-in")

    async def scan_text(self, text: str) -> SecurityResult:
        # 功能: 仅为本地测试合成素材返回审核通过结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        return SecurityResult("local-synthetic-fixture", "PASSED")

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能: 仅为本地测试合成素材返回审核通过结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        if "/fixtures/" not in object_key:
            raise AppError(
                "MEDIA_SECURITY_UNAVAILABLE", "本地安全提供方仅接受 fixtures 合成素材", 503
            )
        return SecurityResult("local-synthetic-fixture", "PASSED")

    async def scan_audio(self, object_key: str) -> SecurityResult:
        # 功能: 仅为本地测试合成素材返回审核通过结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        return await self.scan_image(object_key)

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        # 功能: 仅为本地测试合成素材返回审核通过结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     provider_request_id: 云内容安全提供方返回的音频审核任务标识.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        return SecurityResult(provider_request_id, "PASSED")


class AliyunContentSecurityProvider:
    def __init__(
        self, oss: OssProvider, *, region: str, access_key_id: str, access_key_secret: str
    ) -> None:
        # 功能: 初始化内容安全对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     oss: 读取素材或签发对象地址的 OSS 提供方.
        #     region: 云服务地域标识,例如 cn-shanghai.
        #     access_key_id: 云服务访问凭据标识.
        #     access_key_secret: 云服务访问凭据密钥,不应写入日志.
        # 返回: 无返回值;正常完成表示本次操作成功.
        config = Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region_id=region,
            endpoint=f"green-cip.{region}.aliyuncs.com",
        )
        self.client = Client(config)
        self.oss = oss
        self.options = RuntimeOptions(autoretry=False, read_timeout=15000, connect_timeout=5000)

    async def _call(self, method: Any, request: Any) -> dict[str, Any]:
        # 功能: 在线程中调用阿里云审核 SDK 并校验返回状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     method: 待执行的云服务 SDK 可调用方法.
        #     request: 传给阿里云内容安全 SDK 的审核请求模型.
        # 返回: 已确认 Code=200 的审核响应体,包含 Data 风险或任务信息及 RequestId.
        try:
            response = await asyncio.to_thread(method, request, self.options)
            body: dict[str, Any] = response.body.to_map()
            if body.get("Code") != 200:
                raise ValueError("moderation unavailable")
            return body
        except AppError:
            raise
        except Exception:
            raise AppError("MEDIA_SECURITY_UNAVAILABLE", "阿里云内容安全检查未完成", 503) from None

    async def scan_text(self, text: str) -> SecurityResult:
        # 功能: 审核文本并返回内容安全状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     text: 待审核或合成语音的文本内容.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        body = await self._call(
            self.client.text_moderation_plus_with_options,
            models.TextModerationPlusRequest(
                service="text_standard",
                service_parameters=json.dumps({"content": text}, ensure_ascii=False),
            ),
        )
        return _result(body)

    async def scan_image(self, object_key: str) -> SecurityResult:
        # 功能: 审核 OSS 图片对象并返回内容安全状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        url = await self.oss.sign_get_url(object_key, 300)
        body = await self._call(
            self.client.image_moderation_with_options,
            models.ImageModerationRequest(
                service="baselineCheck", service_parameters=json.dumps({"imageUrl": url})
            ),
        )
        return _result(body)

    async def scan_audio(self, object_key: str) -> SecurityResult:
        # 功能: 提交 OSS 音频审核并返回任务或审核状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        url = await self.oss.sign_get_url(object_key, 600)
        body = await self._call(
            self.client.voice_moderation_with_options,
            models.VoiceModerationRequest(
                service="audio_media_detection", service_parameters=json.dumps({"url": url})
            ),
        )
        task_id = body.get("Data", {}).get("TaskId")
        if not isinstance(task_id, str) or not task_id:
            raise AppError("MEDIA_SECURITY_UNAVAILABLE", "音频内容安全任务未建立", 503)
        return SecurityResult(task_id, "PENDING")

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        # 功能: 查询音频审核任务进度和最终状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     provider_request_id: 云内容安全提供方返回的音频审核任务标识.
        # 返回: 审核任务标识,审核状态及可选风险标签.
        body = await self._call(
            self.client.voice_moderation_result_with_options,
            models.VoiceModerationResultRequest(
                service="audio_media_detection",
                service_parameters=json.dumps({"taskId": provider_request_id}),
            ),
        )
        if body.get("Data", {}).get("RiskLevel") is None:
            return SecurityResult(provider_request_id, "PENDING")
        result = _result(body)
        return SecurityResult(provider_request_id, result.status, result.labels)


def _result(body: dict[str, Any]) -> SecurityResult:
    # 功能: 把阿里云内容安全响应转换为可信审核结果.
    # 参数:
    #     body: 阿里云审核 SDK 返回的响应体映射.
    # 返回: 审核任务标识,审核状态及可选风险标签.
    data = body.get("Data", {})
    risk = data.get("RiskLevel")
    if not isinstance(risk, str):
        raise AppError("MEDIA_SECURITY_UNAVAILABLE", "安全检查未返回可信结论", 503)
    return SecurityResult(str(body.get("RequestId", "")), "PASSED" if risk == "none" else "BLOCKED")


def create_content_security_provider(
    settings: Settings, oss: OssProvider
) -> ContentSecurityProvider:
    # 功能: 按启用开关及环境配置选择内容安全提供方.
    # 参数:
    #     settings: 已加载并校验的服务运行配置.
    #     oss: 读取素材或签发对象地址的 OSS 提供方.
    # 返回: 符合当前审核开关和环境的内容安全提供方.
    if not settings.content_security_enabled:
        return SkippedContentSecurityProvider()
    if settings.content_security_provider == "local":
        return LocalFixtureContentSecurityProvider(
            settings.environment, explicitly_enabled=settings.content_security_local_fixtures_only
        )
    if settings.content_security_provider == "aliyun":
        if not (
            settings.content_security_access_key_id and settings.content_security_access_key_secret
        ):
            raise RuntimeError("Aliyun content security requires dedicated server credentials")
        return AliyunContentSecurityProvider(
            oss,
            region=settings.content_security_region,
            access_key_id=settings.content_security_access_key_id.get_secret_value(),
            access_key_secret=settings.content_security_access_key_secret.get_secret_value(),
        )
    return DisabledContentSecurityProvider()
