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
class FeedbackRound:
    ticket_id: str
    round_number: int
    request_text: str | None
    supplement_text: str | None
    paused_at: datetime | None
    supplied_at: datetime | None


@dataclass(frozen=True, slots=True)
class FeedbackReply:
    ticket_id: str
    template: str
    note: str | None
    admin_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackInternalNote:
    id: str
    ticket_id: str
    admin_id: str
    content: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackAdminListItem:
    id: str
    user_id: str
    category: str
    description: str = field(repr=False)
    status: str
    deadline_at: datetime | None
    sla_state: str
    supplement_rounds: int
    created_at: datetime
    updated_at: datetime

    source: dict[str, Any] = field(default_factory=dict)
    screenshot_status: str = "NONE"
    supplied_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeedbackAdminPage:
    items: tuple[FeedbackAdminListItem, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class FeedbackAdminDetail:
    ticket: FeedbackTicket
    screenshots: tuple[FeedbackScreenshot, ...]
    rounds: tuple[FeedbackRound, ...]
    replies: tuple[FeedbackReply, ...]
    timeline: tuple[FeedbackTimelineEvent, ...]
    internal_notes: tuple[FeedbackInternalNote, ...]


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
    timeline_payload: dict[str, Any] = field(default_factory=dict, repr=False)
    round_request_text: str | None = None
    round_supplement_text: str | None = None
    reply_template: str | None = None
    reply_note: str | None = None
