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
    def use_once(self, service_name: str, nonce: str, ttl_seconds: int) -> Awaitable[bool]:
        # 功能: 原子占用指定服务的签名随机数,拒绝有效期内重复使用.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     service_name: 内部调用方或当前服务的名称.
        #     nonce: 本次签名请求的随机数,同服务在有效窗口内不得重复.
        #     ttl_seconds: 随机数去重记录的保存时长,单位为秒.
        # 返回: 随机数首次成功占用时为 True,已占用时为 False.
        ...


class RedisSetClient(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> Any:
        # 功能: 定义带过期时间和条件写入能力的 Redis 端口.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     name: Redis 去重键的完整名称.
        #     value: Redis 去重键保存的标记内容.
        #     ex: Redis 键过期时长,单位为秒.
        #     nx: 是否仅在 Redis 键不存在时写入.
        # 返回: Redis SET 确认结果;NX 条件未满足时为空,调用方转为布尔值判断占用成功.
        ...


class RedisNonceStore:
    def __init__(self, redis: RedisSetClient, key_prefix: str = "juya:internal:nonce") -> None:
        # 功能: 初始化后台服务对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     redis: 支持带过期时间及 NX 条件写入的 Redis 客户端.
        #     key_prefix: Redis 内部请求随机数去重键的命名空间前缀.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._redis = redis
        self._key_prefix = key_prefix

    async def use_once(self, service_name: str, nonce: str, ttl_seconds: int) -> bool:
        # 功能: 原子占用指定服务的签名随机数,拒绝有效期内重复使用.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     service_name: 内部调用方或当前服务的名称.
        #     nonce: 本次签名请求的随机数,同服务在有效窗口内不得重复.
        #     ttl_seconds: 随机数去重记录的保存时长,单位为秒.
        # 返回: 随机数首次成功占用时为 True,已占用时为 False.
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
    # 功能: 对方法,原始路径,时间戳,随机数和请求体摘要生成 HMAC-SHA256 签名.
    # 参数:
    #     method: 内部 HTTP 请求方法,例如 GET 或 POST.
    #     path_with_query: 包含原始查询字符串的请求路径,用于构造签名规范文本.
    #     timestamp: 内部签名请求的 Unix 时间戳,单位为秒.
    #     nonce: 本次签名请求的随机数,同服务在有效窗口内不得重复.
    #     body: 内部签名请求的原始请求体字节,用于计算 SHA-256 摘要.
    #     secret: 内部服务共享签名密钥,签名时使用其原始字节.
    # 返回: HMAC-SHA256 签名的十六进制字符串.
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (method.upper(), path_with_query, str(timestamp), nonce, body_hash)
    ).encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def _path_with_query(request: Request) -> str:
    # 功能: 保留原始路径和查询字符串构造内部签名路径.
    # 参数:
    #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
    # 返回: 原始请求路径;存在查询串时按原样追加.
    raw_path = request.scope.get("raw_path", request.url.path.encode())
    path = bytes(raw_path).decode("ascii")
    query = bytes(request.scope.get("query_string", b"")).decode("ascii")
    return f"{path}?{query}" if query else path


def _unauthorized(code: str, message: str, *, status_code: int = 401) -> AppError:
    # 功能: 构造内部请求认证失败的业务异常.
    # 参数:
    #     code: 对外返回的稳定业务错误码.
    #     message: 向调用方说明失败原因的错误文本.
    #     status_code: HTTP 响应状态码.
    # 返回: 包含业务码,说明及 HTTP 状态的异常对象.
    return AppError(code=code, message=message, status_code=status_code)


async def verify_request_signature(
    request: Request,
    secret: SecretStr,
    nonce_store: NonceStore,
    now: datetime,
) -> ServicePrincipal:
    # 功能: 检查内部请求签名,时间偏差并占用随机数防止重放.
    # 参数:
    #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
    #     secret: 内部服务共享签名密钥,签名时使用其原始字节.
    #     nonce_store: 提供内部请求随机数一次性占用能力的去重存储.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 经过内部签名认证的调用服务身份.
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
