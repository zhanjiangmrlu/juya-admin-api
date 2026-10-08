import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol, cast

from juya_admin_api.shared.errors import AppError


@dataclass(slots=True)
class IdempotencyRecord:
    scope: str
    actor_id: str
    key: str
    request_hash: str
    status: str = "IN_PROGRESS"
    response_status: int | None = None
    response_body: dict[str, object] | None = None
    is_replay: bool = False


class IdempotencyRepository(Protocol):
    async def create_or_get(self, record: IdempotencyRecord) -> IdempotencyRecord:
        # 功能: 原子创建幂等记录或读取相同作用域中的已有记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待创建或更新的幂等记录,包含作用域,主体,请求摘要及结果.
        # 返回: 当前作用域和主体对应的幂等记录.
        ...

    async def save(self, record: IdempotencyRecord) -> None:
        # 功能: 保存命令幂等记录的当前处理状态和响应.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待创建或更新的幂等记录,包含作用域,主体,请求摘要及结果.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ...


class IdempotencyService:
    def __init__(self, repository: IdempotencyRepository) -> None:
        # 功能: 初始化命令幂等对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供命令幂等持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

    async def begin(
        self, scope: str, actor_id: str, key: str, request_hash: str
    ) -> IdempotencyRecord:
        # 功能: 申请命令幂等记录并检查请求摘要是否一致.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     scope: 幂等记录的业务作用域,隔离不同类型的命令.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 当前作用域和主体对应的幂等记录.
        candidate = IdempotencyRecord(scope, actor_id, key, request_hash)
        record = await self._repository.create_or_get(candidate)
        if record.request_hash != request_hash:
            raise AppError(
                "IDEMPOTENCY_KEY_REUSED",
                "幂等键已用于不同请求",
                409,
            )
        if record is candidate:
            return record
        if record.status != "COMPLETED":
            raise AppError("IDEMPOTENCY_IN_PROGRESS", "相同请求正在处理中", 409)
        record.is_replay = True
        return record

    async def complete(
        self,
        record: IdempotencyRecord,
        response: dict[str, object],
        *,
        status_code: int = 200,
    ) -> None:
        # 功能: 保存幂等命令的响应内容和状态码,标记处理完成.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待创建或更新的幂等记录,包含作用域,主体,请求摘要及结果.
        #     response: 命令已完成的业务响应内容,存入幂等记录供重放.
        #     status_code: HTTP 响应状态码.
        # 返回: 无返回值;正常完成表示本次操作成功.
        record.status = "COMPLETED"
        record.response_status = status_code
        record.response_body = deepcopy(response)
        await self._repository.save(record)


class InMemoryIdempotencyRepository:
    def __init__(self) -> None:
        # 功能: 初始化命令幂等对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def create_or_get(self, record: IdempotencyRecord) -> IdempotencyRecord:
        # 功能: 原子创建幂等记录或读取相同作用域中的已有记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待创建或更新的幂等记录,包含作用域,主体,请求摘要及结果.
        # 返回: 当前作用域和主体对应的幂等记录.
        identity = (record.scope, record.actor_id, record.key)
        async with self._lock:
            existing = self._records.get(identity)
            if existing is not None:
                return existing
            self._records[identity] = record
            return record

    async def save(self, record: IdempotencyRecord) -> None:
        # 功能: 保存命令幂等记录的当前处理状态和响应.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待创建或更新的幂等记录,包含作用域,主体,请求摘要及结果.
        # 返回: 无返回值;正常完成表示本次操作成功.
        identity = (record.scope, record.actor_id, record.key)
        async with self._lock:
            self._records[identity] = record


def _json_body(value: object) -> dict[str, object]:
    # 功能: 把幂等记录的 JSON 响应解析为字典.
    # 参数:
    #     value: 数据库或业务存储中的 JSON 文本或已解码对象.
    # 返回: 幂等记录中可重放的响应字典.
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, object], decoded)
    raise ValueError("Idempotency response body must be a JSON object")
