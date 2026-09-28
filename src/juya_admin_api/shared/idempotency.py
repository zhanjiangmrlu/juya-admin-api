import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

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
    async def create_or_get(self, record: IdempotencyRecord) -> IdempotencyRecord: ...

    async def save(self, record: IdempotencyRecord) -> None: ...


class IdempotencyService:
    def __init__(self, repository: IdempotencyRepository) -> None:
        self._repository = repository

    async def begin(
        self, scope: str, actor_id: str, key: str, request_hash: str
    ) -> IdempotencyRecord:
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
        record.status = "COMPLETED"
        record.response_status = status_code
        record.response_body = deepcopy(response)
        await self._repository.save(record)


class InMemoryIdempotencyRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def create_or_get(self, record: IdempotencyRecord) -> IdempotencyRecord:
        identity = (record.scope, record.actor_id, record.key)
        async with self._lock:
            existing = self._records.get(identity)
            if existing is not None:
                return existing
            self._records[identity] = record
            return record

    async def save(self, record: IdempotencyRecord) -> None:
        identity = (record.scope, record.actor_id, record.key)
        async with self._lock:
            self._records[identity] = record
