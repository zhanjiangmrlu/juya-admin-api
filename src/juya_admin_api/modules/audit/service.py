from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.ids import new_ulid


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


class SQLAlchemyAuditRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audit_event "
                    "(public_id, actor_public_id, action, object_type, object_public_id, "
                    "before_summary, after_summary, reason, request_id, created_at) VALUES "
                    "(:public_id, :actor, :action, :object_type, :object_id, :before_summary, "
                    ":after_summary, :reason, :request_id, :created_at)"
                ),
                {
                    "public_id": new_ulid(event.occurred_at),
                    "actor": event.actor_public_id,
                    "action": event.action,
                    "object_type": event.object_type,
                    "object_id": event.object_public_id,
                    "before_summary": event.before_summary,
                    "after_summary": event.after_summary,
                    "reason": event.reason,
                    "request_id": event.request_id,
                    "created_at": event.occurred_at,
                },
            )

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT actor_public_id, action, object_type, object_public_id, "
                        "before_summary, after_summary, reason, request_id, created_at "
                        "FROM audit_event ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"limit": limit},
                )
            ).all()
        events: list[AuditEvent] = []
        for row in rows:
            occurred_at = row.created_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            events.append(
                AuditEvent(
                    actor_public_id=row.actor_public_id or "system",
                    action=row.action,
                    object_type=row.object_type,
                    object_public_id=row.object_public_id,
                    before_summary=dict(row.before_summary or {}),
                    after_summary=dict(row.after_summary or {}),
                    reason=row.reason,
                    request_id=row.request_id,
                    occurred_at=occurred_at,
                )
            )
        return events
