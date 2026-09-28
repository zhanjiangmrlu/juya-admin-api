from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import Request
from pydantic import SecretStr

from juya_admin_api.infrastructure.security.service_hmac import (
    NonceStore,
    ServicePrincipal,
    verify_request_signature,
)


def create_service_auth_dependency(
    secret: SecretStr, nonce_store: NonceStore
) -> Callable[[Request], object]:
    async def require_service(request: Request) -> ServicePrincipal:
        return await verify_request_signature(request, secret, nonce_store, datetime.now(UTC))

    return require_service
