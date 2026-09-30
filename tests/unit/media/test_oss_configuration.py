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
