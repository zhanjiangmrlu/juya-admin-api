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
        from_environment: bool = False,
    ) -> None:
        self._values = (access_key_id, access_key_secret, security_token)
        self._expires_at = expires_at
        self._from_environment = from_environment or not any(self._values)
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
                if self._from_environment:
                    # Snapshot a complete bundle; never mix primary and legacy sources.
                    environment = dict(os.environ)
                    names = ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET", "SESSION_TOKEN")
                    prefix = (
                        "JUYA_OSS_"
                        if any(environment.get("JUYA_OSS_" + name) for name in names)
                        else "OSS_"
                    )
                    key_id, secret, token = (environment.get(prefix + name) for name in names)
                    expiry_text = environment.get("JUYA_OSS_CREDENTIALS_EXPIRES_AT")
                    expiry = (
                        datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
                        if expiry_text and token
                        else None
                    )
                    if token and (expiry is None or expiry.tzinfo is None):
                        raise ValueError("STS expiration is required")
                else:
                    key_id, secret, token = self._values
                    expiry = self._expires_at
                credentials = oss.types.Credentials(
                    key_id or "",
                    secret or "",
                    token or None,
                    expiry,
                )
        except Exception:
            raise AppError("OSS_CREDENTIALS_UNAVAILABLE", "OSS凭据获取失败", 503) from None
        if not credentials.access_key_id or not credentials.access_key_secret:
            raise AppError("OSS_CREDENTIALS_UNAVAILABLE", "OSS凭据未配置", 503)
        return credentials
