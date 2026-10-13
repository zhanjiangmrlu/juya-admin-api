import os
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(env_prefix="JUYA_", extra="ignore", env_ignore_empty=True)

    environment: str = "local"
    allow_insecure_http: bool = False
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
    signed_url_ttl_seconds: int = Field(default=60, ge=1, le=60)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    import_storage_path: str = "/var/lib/juya/imports"
    tts_enabled: bool = False
    tts_secret_id: SecretStr | None = None
    tts_secret_key: SecretStr | None = None
    tts_session_token: SecretStr | None = None
    tts_voice: int = 101050
    tts_region: str = "ap-guangzhou"
    tts_config_version: int = Field(default=1, ge=1)
    tts_price_version: str = "unconfigured"
    tts_microcost_per_character: int = Field(default=30, ge=0)
    tts_daily_character_limit: int = Field(default=0, ge=0)
    tts_daily_microcost_limit: int = Field(default=0, ge=0)
    asr_enabled: bool = False
    asr_api_key: SecretStr | None = None
    asr_model: str = "qwen3-asr-flash-filetrans"
    asr_audio_hosts: str = ""
    asr_config_version: str = "v1"
    asr_daily_seconds_limit: int = Field(default=0, ge=0)
    content_ai_enabled: bool = False
    content_ai_api_key: SecretStr | None = None
    content_ai_model: str = "qwen-flash"
    content_ai_config_version: str = "v1"
    content_ai_daily_token_limit: int = Field(default=0, ge=0)
    content_ai_max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    content_security_enabled: bool = False
    content_security_provider: Literal["disabled", "aliyun", "local"] = "disabled"
    content_security_region: str = "cn-shanghai"
    content_security_access_key_id: SecretStr | None = None
    content_security_access_key_secret: SecretStr | None = None
    content_security_local_fixtures_only: bool = False
    required_schema_version: int = 25

    @model_validator(mode="after")
    def validate_http_test_mode(self) -> Self:
        # 功能: 阻止非测试环境使用明文 HTTP 会话 Cookie
        # 参数 self 为已加载的运行配置,返回验证后的当前配置
        if self.allow_insecure_http and self.environment != "test":
            raise ValueError("JUYA_ALLOW_INSECURE_HTTP is only allowed in test")
        return self

    def validate_oss_configuration(self) -> None:
        # 功能: 在服务构建前校验 OSS 配置组合,阻止不完整或不安全配置.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
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
