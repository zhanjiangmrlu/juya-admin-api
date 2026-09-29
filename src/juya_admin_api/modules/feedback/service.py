from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

from juya_admin_api.modules.feedback.domain import (
    CommandEffects,
    FeedbackAdminDetail,
    FeedbackAdminPage,
    FeedbackInternalNote,
    FeedbackOutboxMessage,
    FeedbackTicket,
)
from juya_admin_api.modules.feedback.repository import FeedbackRepository
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

_CATEGORIES = {"CONTENT", "PRONUNCIATION", "DISPLAY", "FUNCTION"}


class FeedbackService:
    def __init__(
        self,
        repository: FeedbackRepository,
        *,
        sla_hours_provider: Callable[[], int] = lambda: 48,
    ) -> None:
        self._repository = repository
        self._sla_hours_provider = sla_hours_provider

    async def get(self, ticket_id: str) -> FeedbackTicket:
        ticket = await self._repository.get(ticket_id)
        if ticket is None:
            raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
        return ticket

    async def list_admin(
        self,
        filters: dict[str, str],
        page: int,
        page_size: int,
        now: datetime,
    ) -> FeedbackAdminPage:
        return await self._repository.list_admin(filters, page, page_size, now)

    async def get_admin(self, ticket_id: str) -> FeedbackAdminDetail:
        detail = await self._repository.get_admin(ticket_id)
        if detail is None:
            raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
        return detail

    async def add_internal_note(
        self,
        ticket_id: str,
        admin_id: str,
        content: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackInternalNote:
        if not content.strip() or len(content) > 200:
            raise AppError("FEEDBACK_INTERNAL_NOTE_INVALID", "内部备注最多200字", 422)
        note = FeedbackInternalNote(new_ulid(now), ticket_id, admin_id, content.strip(), now)
        return await self._repository.add_internal_note(note, idempotency_key)

    async def create(
        self,
        user_id: str,
        category: str,
        description: str,
        source: Mapping[str, object],
        screenshots: Sequence[str],
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if category not in _CATEGORIES:
            raise AppError("FEEDBACK_CATEGORY_INVALID", "反馈分类无效", 422)
        if not description.strip():
            raise AppError("FEEDBACK_DESCRIPTION_REQUIRED", "请填写反馈说明", 422)
        if len(description) > 300:
            raise AppError("FEEDBACK_DESCRIPTION_TOO_LONG", "反馈说明最多300字", 422)
        if len(screenshots) > 1:
            raise AppError("FEEDBACK_SCREENSHOT_LIMIT", "每条反馈最多一张截图", 422)
        sla_hours = self._sla_hours()
        ticket = FeedbackTicket(
            id=new_ulid(now),
            user_id=user_id,
            category=category,
            description=description,
            source=dict(source),
            status="PENDING",
            sla_hours=sla_hours,
            deadline_at=now + timedelta(hours=sla_hours),
            sla_remaining_seconds=None,
            supplement_rounds=0,
            reopen_count=0,
            created_at=now,
            updated_at=now,
        )
        return await self._repository.create(ticket, screenshots, idempotency_key)

    async def start_processing(
        self, ticket_id: str, admin_id: str, idempotency_key: str, now: datetime
    ) -> FeedbackTicket:
        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_status(ticket, {"PENDING", "USER_SUPPLIED"})
            ticket.status = "PROCESSING"
            return CommandEffects("PROCESSING_STARTED")

        return await self._repository.apply(
            ticket_id,
            "START_PROCESSING",
            idempotency_key,
            "ADMIN",
            admin_id,
            now,
            mutation,
        )

    async def request_supplement(
        self,
        ticket_id: str,
        request_text: str,
        admin_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if not request_text.strip() or len(request_text) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "补充要求最多200字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_status(ticket, {"PROCESSING"})
            if ticket.supplement_rounds >= 2:
                raise AppError("FEEDBACK_SUPPLEMENT_LIMIT", "最多要求补充两轮", 409)
            remaining = (
                max(0, int((ticket.deadline_at - now).total_seconds()))
                if ticket.deadline_at is not None
                else ticket.sla_hours * 3600
            )
            ticket.supplement_rounds += 1
            ticket.sla_remaining_seconds = remaining
            ticket.deadline_at = None
            ticket.status = "NEED_MORE"
            return CommandEffects(
                "NEED_MORE",
                self._message(ticket, "FEEDBACK_NEED_MORE", "反馈需要补充", "请补充更多信息"),
                timeline_payload={"request_text": request_text},
                round_request_text=request_text,
            )

        return await self._repository.apply(
            ticket_id,
            "REQUEST_SUPPLEMENT",
            idempotency_key,
            "ADMIN",
            admin_id,
            now,
            mutation,
        )

    async def supply(
        self,
        ticket_id: str,
        supplement: str,
        user_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if not supplement.strip() or len(supplement) > 300:
            raise AppError("FEEDBACK_SUPPLEMENT_INVALID", "补充说明最多300字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_owner(ticket, user_id)
            self._require_status(ticket, {"NEED_MORE"})
            sla_hours = self._sla_hours()
            ticket.sla_hours = sla_hours
            ticket.deadline_at = now + timedelta(hours=sla_hours)
            ticket.sla_remaining_seconds = None
            ticket.status = "USER_SUPPLIED"
            return CommandEffects(
                "USER_SUPPLIED",
                timeline_payload={"supplement_text": supplement},
                round_supplement_text=supplement,
            )

        return await self._repository.apply(
            ticket_id, "SUPPLY", idempotency_key, "USER", user_id, now, mutation
        )

    async def resolve(
        self,
        ticket_id: str,
        template: str,
        note: str | None,
        admin_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if note is not None and len(note) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "回复补充说明最多200字", 422)
        if template not in {"RESOLVED", "TEMPORARILY_UNAVAILABLE"}:
            raise AppError("FEEDBACK_TEMPLATE_INVALID", "反馈回复模板无效", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_status(ticket, {"PROCESSING", "USER_SUPPLIED"})
            ticket.status = "RESOLVED"
            ticket.deadline_at = None
            ticket.resolved_at = now
            return CommandEffects(
                "RESOLVED",
                self._message(ticket, "FEEDBACK_RESOLVED", "反馈已有结果", "请查看处理结果"),
                timeline_payload={"template": template},
                reply_template=template,
                reply_note=note,
            )

        return await self._repository.apply(
            ticket_id, "RESOLVE", idempotency_key, "ADMIN", admin_id, now, mutation
        )

    async def reopen(
        self,
        ticket_id: str,
        reason: str,
        user_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if not reason.strip() or len(reason) > 300:
            raise AppError("FEEDBACK_REOPEN_REASON_INVALID", "重开原因最多300字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_owner(ticket, user_id)
            self._require_status(ticket, {"RESOLVED"})
            if ticket.reopen_count >= 1:
                raise AppError("FEEDBACK_REOPEN_LIMIT", "反馈最多重开一次", 409)
            if ticket.resolved_at is None or now > ticket.resolved_at + timedelta(days=7):
                raise AppError("FEEDBACK_REOPEN_WINDOW_EXPIRED", "反馈重开期限已过", 409)
            sla_hours = self._sla_hours()
            ticket.reopen_count += 1
            ticket.sla_hours = sla_hours
            ticket.deadline_at = now + timedelta(hours=sla_hours)
            ticket.resolved_at = None
            ticket.status = "PROCESSING"
            return CommandEffects("REOPENED", timeline_payload={"reason": reason})

        return await self._repository.apply(
            ticket_id, "REOPEN", idempotency_key, "USER", user_id, now, mutation
        )

    async def close_insufficient(
        self,
        ticket_id: str,
        reason: str,
        admin_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> FeedbackTicket:
        if not reason.strip() or len(reason) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "关闭原因最多200字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            self._require_status(ticket, {"PROCESSING", "NEED_MORE", "USER_SUPPLIED"})
            ticket.status = "CLOSED_INSUFFICIENT"
            ticket.deadline_at = None
            ticket.closed_at = now
            return CommandEffects(
                "CLOSED_INSUFFICIENT",
                self._message(ticket, "FEEDBACK_CLOSED", "反馈已关闭", "信息不足 无法继续处理"),
                now + timedelta(days=30),
                timeline_payload={"reason": reason},
                reply_template="CLOSED_INSUFFICIENT",
                reply_note=reason,
            )

        return await self._repository.apply(
            ticket_id,
            "CLOSE_INSUFFICIENT",
            idempotency_key,
            "ADMIN",
            admin_id,
            now,
            mutation,
        )

    def _sla_hours(self) -> int:
        return max(1, min(int(self._sla_hours_provider()), 24 * 30))

    @staticmethod
    def _require_status(ticket: FeedbackTicket, allowed: set[str]) -> None:
        if ticket.status not in allowed:
            raise AppError("FEEDBACK_STATE_CONFLICT", "反馈状态已变化", 409)

    @staticmethod
    def _require_owner(ticket: FeedbackTicket, user_id: str) -> None:
        if ticket.user_id != user_id:
            raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)

    @staticmethod
    def _message(
        ticket: FeedbackTicket, message_type: str, title: str, summary: str
    ) -> FeedbackOutboxMessage:
        event_id = new_ulid(ticket.updated_at)
        return FeedbackOutboxMessage(
            event_id,
            ticket.user_id,
            message_type,
            title,
            summary,
            "FEEDBACK",
            ticket.id,
        )
