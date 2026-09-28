from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Request
from pydantic import SecretStr

from juya_admin_api.infrastructure.security.service_hmac import (
    NonceStore,
    ServicePrincipal,
    verify_request_signature,
)
from juya_admin_api.shared.errors import AppError


def create_service_auth_dependency(
    secret: SecretStr,
    nonce_store: NonceStore,
    *,
    allowed_services: frozenset[str] | None = None,
) -> Callable[[Request], Awaitable[ServicePrincipal]]:
    async def require_service(request: Request) -> ServicePrincipal:
        principal = await verify_request_signature(request, secret, nonce_store, datetime.now(UTC))
        if allowed_services is not None and principal.service_name not in allowed_services:
            raise AppError("INTERNAL_SERVICE_FORBIDDEN", "内部服务无权访问", 403)
        return principal

    return require_service
