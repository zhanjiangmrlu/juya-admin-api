import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.feedback.domain import (
    CommandEffects,
    FeedbackScreenshot,
    FeedbackTicket,
    FeedbackTimelineEvent,
)
from juya_admin_api.shared.errors import AppError

TicketMutation = Callable[[FeedbackTicket], CommandEffects]


class FeedbackRepository(Protocol):
    async def create(
        self,
        ticket: FeedbackTicket,
        screenshots: Sequence[str],
        idempotency_key: str,
    ) -> FeedbackTicket: ...

    async def apply(
        self,
        ticket_id: str,
        command: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        now: datetime,
        mutation: TicketMutation,
    ) -> FeedbackTicket: ...

    async def get(self, ticket_id: str) -> FeedbackTicket | None: ...


class InMemoryFeedbackRepository:
    def __init__(self) -> None:
        self.tickets: dict[str, FeedbackTicket] = {}
        self.screenshots: dict[str, FeedbackScreenshot] = {}
        self.timeline: list[FeedbackTimelineEvent] = []
        self.outbox: dict[str, object] = {}
        self._commands: dict[tuple[str, str, str], FeedbackTicket] = {}
        self._create_keys: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        ticket: FeedbackTicket,
        screenshots: Sequence[str],
        idempotency_key: str,
    ) -> FeedbackTicket:
        async with self._lock:
            identity = (ticket.user_id, idempotency_key)
            existing_id = self._create_keys.get(identity)
            if existing_id is not None:
                return self.tickets[existing_id]
            self.tickets[ticket.id] = ticket
            if screenshots:
                self.screenshots[ticket.id] = FeedbackScreenshot(ticket.id, screenshots[0])
            self.timeline.append(
                FeedbackTimelineEvent(
                    ticket.id, "CREATED", "USER", ticket.user_id, ticket.created_at
                )
            )
            self._create_keys[identity] = ticket.id
            return ticket

    async def apply(
        self,
        ticket_id: str,
        command: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        now: datetime,
        mutation: TicketMutation,
    ) -> FeedbackTicket:
        async with self._lock:
            identity = (ticket_id, command, idempotency_key)
            replay = self._commands.get(identity)
            if replay is not None:
                return replay
            ticket = self.tickets.get(ticket_id)
            if ticket is None:
                raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
            effects = mutation(ticket)
            ticket.updated_at = now
            self.timeline.append(
                FeedbackTimelineEvent(ticket.id, effects.event_type, actor_type, actor_id, now)
            )
            if effects.outbox is not None:
                self.outbox[effects.outbox.event_id] = effects.outbox
            if effects.screenshot_delete_after is not None and ticket.id in self.screenshots:
                self.screenshots[ticket.id].delete_after = effects.screenshot_delete_after
            self._commands[identity] = ticket
            return ticket

    async def get(self, ticket_id: str) -> FeedbackTicket | None:
        return self.tickets.get(ticket_id)


def _database_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SQLAlchemyFeedbackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        ticket: FeedbackTicket,
        screenshots: Sequence[str],
        idempotency_key: str,
    ) -> FeedbackTicket:
        async with self._session_factory() as session, session.begin():
            user_id = await self._lock_user(session, ticket.user_id)
            existing = await self._load_by_create_key(session, user_id, idempotency_key)
            if existing is not None:
                return existing
            await session.execute(
                text(
                    "INSERT INTO feedback_ticket "
                    "(public_id, user_id, category, description, source, status, sla_hours, "
                    "deadline_at, sla_remaining_seconds, supplement_rounds, reopen_count, "
                    "create_idempotency_key, created_at, updated_at, resolved_at, closed_at) "
                    "VALUES (:public_id, :user_id, :category, :description, :source, :status, "
                    ":sla_hours, :deadline_at, :remaining, :rounds, :reopen_count, :create_key, "
                    ":created_at, :updated_at, :resolved_at, :closed_at)"
                ),
                {
                    "public_id": ticket.id,
                    "user_id": user_id,
                    "category": ticket.category,
                    "description": ticket.description,
                    "source": json.dumps(ticket.source, ensure_ascii=False, separators=(",", ":")),
                    "status": ticket.status,
                    "sla_hours": ticket.sla_hours,
                    "deadline_at": _database_datetime(ticket.deadline_at),
                    "remaining": ticket.sla_remaining_seconds,
                    "rounds": ticket.supplement_rounds,
                    "reopen_count": ticket.reopen_count,
                    "create_key": idempotency_key,
                    "created_at": _database_datetime(ticket.created_at),
                    "updated_at": _database_datetime(ticket.updated_at),
                    "resolved_at": _database_datetime(ticket.resolved_at),
                    "closed_at": _database_datetime(ticket.closed_at),
                },
            )
            internal_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            if internal_id is None:
                raise RuntimeError("Feedback insert did not return an id")
            if screenshots:
                await session.execute(
                    text(
                        "INSERT INTO feedback_screenshot "
                        "(ticket_id, object_key, security_status) "
                        "VALUES (:ticket_id, :object_key, 'PASSED')"
                    ),
                    {"ticket_id": internal_id, "object_key": screenshots[0]},
                )
            await self._insert_timeline(
                session, int(internal_id), "CREATED", "USER", ticket.user_id, ticket.created_at
            )
            return ticket

    async def apply(
        self,
        ticket_id: str,
        command: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        now: datetime,
        mutation: TicketMutation,
    ) -> FeedbackTicket:
        async with self._session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT ft.id, ft.public_id, u.public_id AS user_public_id, "
                            "ft.category, ft.description, ft.source, ft.status, ft.sla_hours, "
                            "ft.deadline_at, ft.sla_remaining_seconds, ft.supplement_rounds, "
                            "ft.reopen_count, ft.created_at, ft.updated_at, ft.resolved_at, "
                            "ft.closed_at FROM feedback_ticket ft "
                            "JOIN user_account u ON u.id = ft.user_id "
                            "WHERE ft.public_id = :public_id FOR UPDATE"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
            replay = await session.scalar(
                text(
                    "SELECT id FROM feedback_command WHERE ticket_id = :ticket_id "
                    "AND command = :command AND idempotency_key = :idempotency_key FOR UPDATE"
                ),
                {
                    "ticket_id": row["id"],
                    "command": command,
                    "idempotency_key": idempotency_key,
                },
            )
            ticket = self._from_row(row)
            if replay is not None:
                return ticket
            effects = mutation(ticket)
            ticket.updated_at = now
            await session.execute(
                text(
                    "UPDATE feedback_ticket SET status = :status, sla_hours = :sla_hours, "
                    "deadline_at = :deadline_at, sla_remaining_seconds = :remaining, "
                    "supplement_rounds = :rounds, reopen_count = :reopen_count, "
                    "updated_at = :updated_at, resolved_at = :resolved_at, "
                    "closed_at = :closed_at WHERE id = :id"
                ),
                {
                    "status": ticket.status,
                    "sla_hours": ticket.sla_hours,
                    "deadline_at": _database_datetime(ticket.deadline_at),
                    "remaining": ticket.sla_remaining_seconds,
                    "rounds": ticket.supplement_rounds,
                    "reopen_count": ticket.reopen_count,
                    "updated_at": _database_datetime(ticket.updated_at),
                    "resolved_at": _database_datetime(ticket.resolved_at),
                    "closed_at": _database_datetime(ticket.closed_at),
                    "id": row["id"],
                },
            )
            await self._insert_timeline(
                session, row["id"], effects.event_type, actor_type, actor_id, now
            )
            if effects.outbox is not None:
                payload = asdict(effects.outbox)
                await session.execute(
                    text(
                        "INSERT INTO admin_outbox "
                        "(event_id, event_type, aggregate_public_id, payload, status, "
                        "next_attempt_at) VALUES (:event_id, 'MINIAPP_MESSAGE', :aggregate_id, "
                        ":payload, 'PENDING', :now)"
                    ),
                    {
                        "event_id": effects.outbox.event_id,
                        "aggregate_id": ticket.id,
                        "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        "now": _database_datetime(now),
                    },
                )
            if effects.screenshot_delete_after is not None:
                await session.execute(
                    text(
                        "UPDATE feedback_screenshot SET delete_after = :delete_after "
                        "WHERE ticket_id = :ticket_id"
                    ),
                    {
                        "delete_after": _database_datetime(effects.screenshot_delete_after),
                        "ticket_id": row["id"],
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO feedback_command "
                    "(ticket_id, command, idempotency_key, created_at) "
                    "VALUES (:ticket_id, :command, :idempotency_key, :now)"
                ),
                {
                    "ticket_id": row["id"],
                    "command": command,
                    "idempotency_key": idempotency_key,
                    "now": _database_datetime(now),
                },
            )
            return ticket

    async def get(self, ticket_id: str) -> FeedbackTicket | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT ft.public_id, u.public_id AS user_public_id, ft.category, "
                            "ft.description, ft.source, ft.status, ft.sla_hours, ft.deadline_at, "
                            "ft.sla_remaining_seconds, ft.supplement_rounds, ft.reopen_count, "
                            "ft.created_at, ft.updated_at, ft.resolved_at, ft.closed_at "
                            "FROM feedback_ticket ft JOIN user_account u ON u.id = ft.user_id "
                            "WHERE ft.public_id = :public_id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._from_row(row) if row is not None else None

    @staticmethod
    async def _lock_user(session: AsyncSession, public_id: str) -> int:
        internal_id = await session.scalar(
            text("SELECT id FROM user_account WHERE public_id = :public_id FOR UPDATE"),
            {"public_id": public_id},
        )
        if internal_id is None:
            raise AppError("USER_NOT_FOUND", "用户不存在", 404)
        return int(internal_id)

    @classmethod
    async def _load_by_create_key(
        cls, session: AsyncSession, user_id: int, idempotency_key: str
    ) -> FeedbackTicket | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT ft.public_id, u.public_id AS user_public_id, ft.category, "
                        "ft.description, ft.source, ft.status, ft.sla_hours, ft.deadline_at, "
                        "ft.sla_remaining_seconds, ft.supplement_rounds, ft.reopen_count, "
                        "ft.created_at, ft.updated_at, ft.resolved_at, ft.closed_at "
                        "FROM feedback_ticket ft JOIN user_account u ON u.id = ft.user_id "
                        "WHERE ft.user_id = :user_id AND ft.create_idempotency_key = :create_key"
                    ),
                    {"user_id": user_id, "create_key": idempotency_key},
                )
            )
            .mappings()
            .first()
        )
        return cls._from_row(row) if row is not None else None

    @staticmethod
    async def _insert_timeline(
        session: AsyncSession,
        ticket_id: int,
        event_type: str,
        actor_type: str,
        actor_id: str,
        now: datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO feedback_timeline "
                "(ticket_id, event_type, actor_type, actor_id, visibility, payload, occurred_at) "
                "VALUES (:ticket_id, :event_type, :actor_type, :actor_id, 'BOTH', '{}', :now)"
            ),
            {
                "ticket_id": ticket_id,
                "event_type": event_type,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "now": _database_datetime(now),
            },
        )

    @staticmethod
    def _from_row(row: RowMapping) -> FeedbackTicket:
        raw_source = row["source"]
        source = json.loads(raw_source) if isinstance(raw_source, str) else raw_source
        created_at = _utc_datetime(row["created_at"])
        updated_at = _utc_datetime(row["updated_at"])
        if created_at is None or updated_at is None:
            raise RuntimeError("Feedback timestamps cannot be null")
        return FeedbackTicket(
            id=row["public_id"],
            user_id=row["user_public_id"],
            category=row["category"],
            description=row["description"],
            source=source,
            status=row["status"],
            sla_hours=row["sla_hours"],
            deadline_at=_utc_datetime(row["deadline_at"]),
            sla_remaining_seconds=row["sla_remaining_seconds"],
            supplement_rounds=row["supplement_rounds"],
            reopen_count=row["reopen_count"],
            created_at=created_at,
            updated_at=updated_at,
            resolved_at=_utc_datetime(row["resolved_at"]),
            closed_at=_utc_datetime(row["closed_at"]),
        )
