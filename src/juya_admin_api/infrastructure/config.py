import os
from datetime import UTC, datetime
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(env_prefix="JUYA_", extra="ignore", env_ignore_empty=True)

    environment: str = "local"
    service_name: str = "juya-admin-api"
    log_level: str = "INFO"
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    internal_hmac_secret: SecretStr | None = None
    allowed_internal_services: str = "juya-miniapp-api"
    miniapp_api_base_url: str = "http://juya-miniapp-api:8000"
    oss_region: str | None = None
    oss_bucket: str | None = None
    oss_endpoint: str | None = None
    oss_expected_bucket: str | None = None
    oss_credentials_mode: Literal["environment", "ecs_ram_role"] = "environment"
    oss_ram_role_name: str | None = None
    oss_access_key_id: SecretStr | None = None
    oss_access_key_secret: SecretStr | None = None
    oss_session_token: SecretStr | None = None
    oss_credentials_expires_at: datetime | None = None
    signed_url_ttl_seconds: int = 300
    ffprobe_path: str = "ffprobe"
    ocr_provider: Literal["disabled", "baidu"] = "disabled"
    baidu_ocr_api_key: SecretStr | None = None
    baidu_ocr_secret_key: SecretStr | None = None
    content_security_enabled: bool = False
    content_security_provider: Literal["disabled", "aliyun", "local"] = "disabled"
    content_security_region: str = "cn-shanghai"
    content_security_access_key_id: SecretStr | None = None
    content_security_access_key_secret: SecretStr | None = None
    content_security_local_fixtures_only: bool = False
    required_schema_version: int = 15

    def validate_oss_configuration(self) -> None:
        """Fail closed before allocating services; errors never contain setting values."""
        if self.oss_expected_bucket and self.oss_bucket != self.oss_expected_bucket:
            raise RuntimeError("OSS bucket does not match the expected environment bucket")
        if not self.oss_region or not self.oss_bucket or not self.oss_expected_bucket:
            raise RuntimeError("OSS requires JUYA_OSS_REGION, BUCKET and EXPECTED_BUCKET")
        if self.oss_credentials_mode == "ecs_ram_role":
            if not self.oss_ram_role_name:
                raise RuntimeError("OSS requires JUYA_OSS_RAM_ROLE_NAME")
            return
        if not (self.oss_access_key_id or os.getenv("OSS_ACCESS_KEY_ID")) or not (
            self.oss_access_key_secret or os.getenv("OSS_ACCESS_KEY_SECRET")
        ):
            raise RuntimeError("OSS server credentials are required")
        token = self.oss_session_token or os.getenv("OSS_SESSION_TOKEN")
        if token:
            expiry = self.oss_credentials_expires_at
            if expiry is None or expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise RuntimeError("OSS STS credentials require a future UTC expiration")
