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
    # 功能: 创建内部服务签名校验及服务白名单依赖.
    # 参数:
    #     secret: 内部服务共享签名密钥,签名时使用其原始字节.
    #     nonce_store: 提供内部请求随机数一次性占用能力的去重存储.
    #     allowed_services: 允许访问内部接口的服务名称集合;None 表示不额外限制.
    # 返回: 异步内部服务认证依赖,接收请求并返回已验证身份.
    async def require_service(request: Request) -> ServicePrincipal:
        # 功能: 校验内部请求签名并确认调用方属于允许的服务.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        # 返回: 经过内部签名认证的调用服务身份.
        principal = await verify_request_signature(request, secret, nonce_store, datetime.now(UTC))
        if allowed_services is not None and principal.service_name not in allowed_services:
            raise AppError("INTERNAL_SERVICE_FORBIDDEN", "内部服务无权访问", 403)
        return principal

    return require_service
