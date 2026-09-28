from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class FeedbackTicket:
    id: str
    user_id: str
    category: str
    description: str = field(repr=False)
    source: dict[str, Any]
    status: str
    sla_hours: int
    deadline_at: datetime | None
    sla_remaining_seconds: int | None
    supplement_rounds: int
    reopen_count: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(slots=True)
class FeedbackScreenshot:
    ticket_id: str
    object_key: str = field(repr=False)
    security_status: str = "PASSED"
    delete_after: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeedbackTimelineEvent:
    ticket_id: str
    event_type: str
    actor_type: str
    actor_id: str
    occurred_at: datetime
    visibility: str = "BOTH"
    payload: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class FeedbackOutboxMessage:
    event_id: str
    user_id: str
    message_type: str
    title: str
    summary: str
    related_type: str
    related_id: str


@dataclass(frozen=True, slots=True)
class CommandEffects:
    event_type: str
    outbox: FeedbackOutboxMessage | None = None
    screenshot_delete_after: datetime | None = None
