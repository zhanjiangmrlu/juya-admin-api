from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(env_prefix="JUYA_", extra="ignore")

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
    signed_url_ttl_seconds: int = 300
    required_schema_version: int = 11
