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


class DisabledContentSecurityProvider:
    async def scan_text(self, text: str) -> SecurityResult:
        raise AppError("MEDIA_SECURITY_UNAVAILABLE", "未配置独立阿里云内容安全提供方", 503)

    async def scan_image(self, object_key: str) -> SecurityResult:
        return await self.scan_text(object_key)

    async def scan_audio(self, object_key: str) -> SecurityResult:
        return await self.scan_text(object_key)

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        return await self.scan_text(provider_request_id)


class LocalFixtureContentSecurityProvider:
    def __init__(self, environment: str, *, explicitly_enabled: bool) -> None:
        if environment not in {"local", "test"} or not explicitly_enabled:
            raise RuntimeError("local content security requires explicit local/test fixture opt-in")

    async def scan_text(self, text: str) -> SecurityResult:
        return SecurityResult("local-synthetic-fixture", "PASSED")

    async def scan_image(self, object_key: str) -> SecurityResult:
        if "/fixtures/" not in object_key:
            raise AppError(
                "MEDIA_SECURITY_UNAVAILABLE", "本地安全提供方仅接受 fixtures 合成素材", 503
            )
        return SecurityResult("local-synthetic-fixture", "PASSED")

    async def scan_audio(self, object_key: str) -> SecurityResult:
        return await self.scan_image(object_key)

    async def poll_audio(self, provider_request_id: str) -> SecurityResult:
        return SecurityResult(provider_request_id, "PASSED")


class AliyunContentSecurityProvider:
    def __init__(
        self, oss: OssProvider, *, region: str, access_key_id: str, access_key_secret: str
    ) -> None:
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
        body = await self._call(
            self.client.text_moderation_plus_with_options,
            models.TextModerationPlusRequest(
                service="text_standard",
                service_parameters=json.dumps({"content": text}, ensure_ascii=False),
            ),
        )
        return _result(body)

    async def scan_image(self, object_key: str) -> SecurityResult:
        url = await self.oss.sign_get_url(object_key, 300)
        body = await self._call(
            self.client.image_moderation_with_options,
            models.ImageModerationRequest(
                service="baselineCheck", service_parameters=json.dumps({"imageUrl": url})
            ),
        )
        return _result(body)

    async def scan_audio(self, object_key: str) -> SecurityResult:
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
    data = body.get("Data", {})
    risk = data.get("RiskLevel")
    if not isinstance(risk, str):
        raise AppError("MEDIA_SECURITY_UNAVAILABLE", "安全检查未返回可信结论", 503)
    return SecurityResult(str(body.get("RequestId", "")), "PASSED" if risk == "none" else "BLOCKED")


def create_content_security_provider(
    settings: Settings, oss: OssProvider
) -> ContentSecurityProvider:
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
