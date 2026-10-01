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
        return "https://private-oss.test/image?signature=fixture"


@pytest.mark.asyncio
async def test_aliyun_gate_requires_explicit_safe_provider_conclusion() -> None:
    provider = AliyunContentSecurityProvider(
        SignedOss(), region="cn-shanghai", access_key_id="fixture", access_key_secret="fixture"
    )
    for risk in ["none", "low", "medium", "high", None]:

        def call(request: Any, options: Any, risk: str | None = risk) -> Any:
            assert options.autoretry is False
            assert request.service == "baselineCheck"
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
