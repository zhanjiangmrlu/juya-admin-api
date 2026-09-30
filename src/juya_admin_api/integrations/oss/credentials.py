import os
from datetime import UTC, datetime
from typing import Any

import alibabacloud_oss_v2 as oss  # type: ignore[import-untyped]

from juya_admin_api.infrastructure.observability.logging import protect_sdk_logging
from juya_admin_api.shared.errors import AppError


class ControlledCredentialsProvider:
    """Read server credentials per operation, or refresh an explicit ECS RAM role."""

    def __init__(
        self,
        *,
        mode: str = "environment",
        role_name: str | None = None,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        security_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        self._values = (access_key_id, access_key_secret, security_token)
        self._expires_at = expires_at
        self._role: Any = None
        if mode == "ecs_ram_role":
            from alibabacloud_credentials.provider import (  # type: ignore[import-untyped]
                EcsRamRoleCredentialsProvider,
            )

            self._role = EcsRamRoleCredentialsProvider(
                role_name=role_name, disable_imds_v1=True, async_update_enabled=False
            )
        elif mode != "environment":
            raise RuntimeError("OSS credentials mode is invalid")
        protect_sdk_logging()

    def get_credentials(self) -> Any:
        try:
            if self._role is not None:
                source = self._role.get_credentials()
                expiry = source.get_expiration()
                credentials = oss.types.Credentials(
                    source.get_access_key_id(),
                    source.get_access_key_secret(),
                    source.get_security_token(),
                    datetime.fromtimestamp(expiry, UTC) if expiry else None,
                )
            else:
                key_id, secret, token = self._values
                credentials = oss.types.Credentials(
                    key_id or os.getenv("OSS_ACCESS_KEY_ID", ""),
                    secret or os.getenv("OSS_ACCESS_KEY_SECRET", ""),
                    token or os.getenv("OSS_SESSION_TOKEN") or None,
                    self._expires_at,
                )
        except Exception:
            raise AppError("OSS_CREDENTIALS_UNAVAILABLE", "OSS凭据获取失败", 503) from None
        if not credentials.access_key_id or not credentials.access_key_secret:
            raise AppError("OSS_CREDENTIALS_UNAVAILABLE", "OSS凭据未配置", 503)
        return credentials
