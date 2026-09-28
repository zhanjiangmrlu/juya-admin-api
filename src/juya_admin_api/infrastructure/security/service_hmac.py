import hashlib
import hmac
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import SecretStr
from starlette.requests import Request

from juya_admin_api.shared.errors import AppError

INTERNAL_SIGNATURE_MAX_SKEW_SECONDS = 300
INTERNAL_NONCE_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    service_name: str


class NonceStore(Protocol):
    def use_once(self, service_name: str, nonce: str, ttl_seconds: int) -> Awaitable[bool]: ...


class RedisSetClient(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> Any: ...


class RedisNonceStore:
    def __init__(self, redis: RedisSetClient, key_prefix: str = "juya:internal:nonce") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def use_once(self, service_name: str, nonce: str, ttl_seconds: int) -> bool:
        key = f"{self._key_prefix}:{service_name}:{nonce}"
        result = await self._redis.set(key, "1", ex=ttl_seconds, nx=True)
        return bool(result)


def sign_request(
    method: str,
    path_with_query: str,
    timestamp: int,
    nonce: str,
    body: bytes,
    secret: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (method.upper(), path_with_query, str(timestamp), nonce, body_hash)
    ).encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def _path_with_query(request: Request) -> str:
    raw_path = request.scope.get("raw_path", request.url.path.encode())
    path = bytes(raw_path).decode("ascii")
    query = bytes(request.scope.get("query_string", b"")).decode("ascii")
    return f"{path}?{query}" if query else path


def _unauthorized(code: str, message: str, *, status_code: int = 401) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)


async def verify_request_signature(
    request: Request,
    secret: SecretStr,
    nonce_store: NonceStore,
    now: datetime,
) -> ServicePrincipal:
    service_name = request.headers.get("X-Juya-Service", "")
    timestamp_value = request.headers.get("X-Juya-Timestamp", "")
    nonce = request.headers.get("X-Juya-Nonce", "")
    provided_signature = request.headers.get("X-Juya-Signature", "")
    if not service_name or not timestamp_value or not nonce or not provided_signature:
        raise _unauthorized("INVALID_INTERNAL_SIGNATURE", "内部请求签名无效")

    try:
        timestamp = int(timestamp_value)
    except ValueError as error:
        raise _unauthorized("INVALID_INTERNAL_SIGNATURE", "内部请求签名无效") from error

    normalized_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if abs(int(normalized_now.timestamp()) - timestamp) > INTERNAL_SIGNATURE_MAX_SKEW_SECONDS:
        raise _unauthorized("INTERNAL_SIGNATURE_EXPIRED", "内部请求签名时间戳已过期")

    body = await request.body()
    expected_signature = sign_request(
        request.method,
        _path_with_query(request),
        timestamp,
        nonce,
        body,
        secret.get_secret_value().encode(),
    )
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise _unauthorized("INVALID_INTERNAL_SIGNATURE", "内部请求签名无效")

    if not await nonce_store.use_once(service_name, nonce, INTERNAL_NONCE_TTL_SECONDS):
        raise _unauthorized(
            "INTERNAL_REQUEST_REPLAYED",
            "内部请求已处理",
            status_code=409,
        )
    return ServicePrincipal(service_name=service_name)
