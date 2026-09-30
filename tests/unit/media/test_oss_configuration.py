from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app


def test_production_cannot_start_as_health_only_without_oss_configuration() -> None:
    with pytest.raises(RuntimeError, match="OSS"):
        create_app(Settings(environment="production"))


@pytest.mark.parametrize(
    "environment,bucket,expected",
    [
        ("test", "juya", "juya-test"),
        ("production", "juya-test", "juya"),
    ],
)
def test_explicit_bucket_binding_rejects_cross_environment_configuration(
    environment: str,
    bucket: str,
    expected: str,
) -> None:
    settings = Settings(
        environment=environment,
        oss_region="cn-shenzhen",
        oss_bucket=bucket,
        oss_expected_bucket=expected,
    )
    with pytest.raises(RuntimeError, match="bucket"):
        settings.validate_oss_configuration()


def test_empty_optional_sts_expiration_is_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("JUYA_OSS_CREDENTIALS_EXPIRES_AT", "")
    assert Settings().oss_credentials_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["JUYA_OSS_", "OSS_"])
async def test_runtime_rotates_complete_environment_sts_bundle(monkeypatch, prefix: str) -> None:
    from juya_admin_api.infrastructure import runtime as wiring
    from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider

    now = datetime.now(UTC)
    for name in ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET", "SESSION_TOKEN"):
        monkeypatch.delenv("JUYA_OSS_" + name, raising=False)
        monkeypatch.delenv("OSS_" + name, raising=False)
    monkeypatch.setenv(prefix + "ACCESS_KEY_ID", "old-id")
    monkeypatch.setenv(prefix + "ACCESS_KEY_SECRET", "old-secret")
    monkeypatch.setenv(prefix + "SESSION_TOKEN", "old-token")
    monkeypatch.setenv("JUYA_OSS_CREDENTIALS_EXPIRES_AT", (now + timedelta(seconds=90)).isoformat())
    captured: list[AliyunOssProvider] = []

    def capture(*args: Any, **kwargs: Any) -> AliyunOssProvider:
        provider = AliyunOssProvider(*args, **kwargs, clock=lambda: now)
        captured.append(provider)
        return provider

    monkeypatch.setattr(wiring, "AliyunOssProvider", capture)
    resources = wiring.build_runtime(
        Settings(
            environment="test",
            database_url="mysql+asyncmy://test:test@localhost/test",
            redis_url="redis://localhost:6399/0",
            internal_hmac_secret="x" * 32,
            oss_region="cn-shenzhen",
            oss_bucket="juya-test",
            oss_expected_bucket="juya-test",
        )
    )
    try:
        first = await captured[0].create_upload_policy("uploads/images/admin-1/", 1024, 300)
        monkeypatch.setenv(prefix + "ACCESS_KEY_ID", "new-id")
        monkeypatch.setenv(prefix + "ACCESS_KEY_SECRET", "new-secret")
        monkeypatch.setenv(prefix + "SESSION_TOKEN", "new-token")
        monkeypatch.setenv(
            "JUYA_OSS_CREDENTIALS_EXPIRES_AT", (now + timedelta(seconds=900)).isoformat()
        )
        second = await captured[0].create_upload_policy("uploads/images/admin-1/", 1024, 300)
        assert first.fields["x-oss-credential"].startswith("old-id/")
        assert first.expires_in == 60
        assert second.fields["x-oss-credential"].startswith("new-id/")
        assert second.fields["x-oss-security-token"] == "new-token"
        assert second.expires_in == 300
    finally:
        await resources.close()
