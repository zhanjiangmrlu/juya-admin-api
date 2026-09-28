from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuditEvent:
    actor_public_id: str
    action: str
    object_type: str
    object_public_id: str
    before_summary: dict[str, object]
    after_summary: dict[str, object]
    reason: str | None
    request_id: str
    occurred_at: datetime


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None: ...

    async def list_recent(self, limit: int) -> list[AuditEvent]: ...


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    @staticmethod
    def summarize(
        values: dict[str, object], *, allowed_fields: frozenset[str]
    ) -> dict[str, object]:
        return {key: values[key] for key in sorted(allowed_fields) if key in values}

    async def record(self, event: AuditEvent) -> None:
        await self._repository.append(event)

    async def list_recent(self, limit: int = 100) -> list[AuditEvent]:
        return await self._repository.list_recent(min(max(limit, 1), 200))
