import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.analytics.lifecycle import feedback_event
from juya_admin_api.modules.feedback.domain import (
    CommandEffects,
    FeedbackAdminDetail,
    FeedbackAdminListItem,
    FeedbackAdminPage,
    FeedbackInternalNote,
    FeedbackReply,
    FeedbackRound,
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
    ) -> FeedbackTicket:
        # 功能: 校验并创建反馈工单,保存截图和创建时间线,同时保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     screenshots: 反馈关联的截图 OSS 对象键序列,每次提交最多一张.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 当前或变更后的反馈工单.
        ...

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
        # 功能: 锁定反馈工单并执行状态变更及关联副作用,记录幂等结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     command: 反馈状态变更命令名称,纳入幂等隔离.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     actor_type: 执行操作的主体类型,例如 ADMIN 或 USER.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     mutation: 对锁定反馈执行状态变更并生成时间线,通知等副作用的回调.
        # 返回: 当前或变更后的反馈工单.
        ...

    async def get(self, ticket_id: str) -> FeedbackTicket | None:
        # 功能: 读取指定反馈工单记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 匹配的反馈工单;不存在时为 None.
        ...

    async def list_admin(
        self,
        filters: dict[str, str],
        page: int,
        page_size: int,
        now: datetime,
    ) -> FeedbackAdminPage:
        # 功能: 按条件分页查询管理端反馈列表及总数.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 反馈管理列表项及分页统计.
        ...

    async def get_admin(self, ticket_id: str) -> FeedbackAdminDetail | None:
        # 功能: 查询管理端反馈详情,回复,时间线和内部备注.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 管理端反馈详情;不存在时为 None.
        ...

    async def add_internal_note(
        self, note: FeedbackInternalNote, idempotency_key: str
    ) -> FeedbackInternalNote:
        # 功能: 保存仅管理员可见的反馈备注并保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     note: 待保存的反馈内部备注领域对象.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 已保存的内部备注.
        ...


class InMemoryFeedbackRepository:
    def __init__(self) -> None:
        # 功能: 初始化反馈工单对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self.tickets: dict[str, FeedbackTicket] = {}
        self.screenshots: dict[str, FeedbackScreenshot] = {}
        self.timeline: list[FeedbackTimelineEvent] = []
        self.rounds: dict[str, list[FeedbackRound]] = {}
        self.replies: dict[str, list[FeedbackReply]] = {}
        self.internal_notes: dict[str, list[FeedbackInternalNote]] = {}
        self.outbox: dict[str, object] = {}
        self._commands: dict[tuple[str, str, str], FeedbackTicket] = {}
        self._create_keys: dict[tuple[str, str], str] = {}
        self._note_keys: dict[tuple[str, str, str], FeedbackInternalNote] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        ticket: FeedbackTicket,
        screenshots: Sequence[str],
        idempotency_key: str,
    ) -> FeedbackTicket:
        # 功能: 校验并创建反馈工单,保存截图和创建时间线,同时保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     screenshots: 反馈关联的截图 OSS 对象键序列,每次提交最多一张.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 当前或变更后的反馈工单.
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
            self.rounds[ticket.id] = []
            self.replies[ticket.id] = []
            self.internal_notes[ticket.id] = []
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
        # 功能: 锁定反馈工单并执行状态变更及关联副作用,记录幂等结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     command: 反馈状态变更命令名称,纳入幂等隔离.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     actor_type: 执行操作的主体类型,例如 ADMIN 或 USER.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     mutation: 对锁定反馈执行状态变更并生成时间线,通知等副作用的回调.
        # 返回: 当前或变更后的反馈工单.
        async with self._lock:
            identity = (ticket_id, command, idempotency_key)
            replay = self._commands.get(identity)
            if replay is not None:
                return replay
            ticket = self.tickets.get(ticket_id)
            if ticket is None:
                raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
            ticket = replace(ticket)
            effects = mutation(ticket)
            if effects.screenshot_object_key is not None:
                if ticket.id in self.screenshots:
                    raise AppError("FEEDBACK_SCREENSHOT_LIMIT", "每条反馈最多上传1张截图", 422)
                self.screenshots[ticket.id] = FeedbackScreenshot(
                    ticket.id, effects.screenshot_object_key
                )
            self.tickets[ticket.id] = ticket
            ticket.updated_at = now
            self.timeline.append(
                FeedbackTimelineEvent(
                    ticket.id,
                    effects.event_type,
                    actor_type,
                    actor_id,
                    now,
                    payload=effects.timeline_payload,
                )
            )
            if effects.round_request_text is not None:
                self.rounds[ticket.id].append(
                    FeedbackRound(
                        ticket.id,
                        ticket.supplement_rounds,
                        effects.round_request_text,
                        None,
                        now,
                        None,
                    )
                )
            if effects.round_supplement_text is not None:
                current_round = self.rounds[ticket.id][-1]
                self.rounds[ticket.id][-1] = replace(
                    current_round,
                    supplement_text=effects.round_supplement_text,
                    supplied_at=now,
                )
            if effects.reply_template is not None:
                self.replies[ticket.id].append(
                    FeedbackReply(
                        ticket.id,
                        effects.reply_template,
                        effects.reply_note,
                        actor_id,
                        now,
                    )
                )
            if effects.outbox is not None:
                self.outbox[effects.outbox.event_id] = effects.outbox
            if effects.screenshot_delete_after is not None and ticket.id in self.screenshots:
                self.screenshots[ticket.id].delete_after = effects.screenshot_delete_after
            self._commands[identity] = ticket
            return ticket

    async def get(self, ticket_id: str) -> FeedbackTicket | None:
        # 功能: 读取指定反馈工单记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 匹配的反馈工单;不存在时为 None.
        return self.tickets.get(ticket_id)

    async def list_admin(
        self,
        filters: dict[str, str],
        page: int,
        page_size: int,
        now: datetime,
    ) -> FeedbackAdminPage:
        # 功能: 按条件分页查询管理端反馈列表及总数.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 反馈管理列表项及分页统计.
        _validate_admin_query(filters, page, page_size)
        items = [
            replace(
                _list_item(ticket, now),
                screenshot_status=(
                    "DELETED"
                    if self.screenshots[ticket.id].deleted_at
                    else self.screenshots[ticket.id].security_status
                )
                if ticket.id in self.screenshots
                else "NONE",
                supplied_at=max(
                    (r.supplied_at for r in self.rounds.get(ticket.id, []) if r.supplied_at),
                    default=None,
                ),
            )
            for ticket in self.tickets.values()
            if _matches_admin_filters(ticket, filters, now)
        ]
        # 匿名函数: 按超时,用户已补充及更新时间排列管理端反馈列表.
        # 参数:
        #     item: 反馈管理列表项, 含 SLA 状态,工单状态及更新时间.
        # 返回: 超时标志,已补充标志,更新时间和工单标识组成的排序元组.
        items.sort(
            key=lambda item: (
                item.sla_state == "OVERDUE",
                item.status == "USER_SUPPLIED",
                item.updated_at,
                item.id,
            ),
            reverse=True,
        )
        offset = (page - 1) * page_size
        return FeedbackAdminPage(
            tuple(items[offset : offset + page_size]), page, page_size, len(items)
        )

    async def get_admin(self, ticket_id: str) -> FeedbackAdminDetail | None:
        # 功能: 查询管理端反馈详情,回复,时间线和内部备注.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 管理端反馈详情;不存在时为 None.
        ticket = self.tickets.get(ticket_id)
        if ticket is None:
            return None
        # 匿名函数: 为反馈时间线提供按事件发生时间排序的键.
        # 参数:
        #     event: 用户或管理员执行操作产生的反馈时间线事件.
        # 返回: 该事件的发生时间.
        events = tuple(
            sorted(
                (event for event in self.timeline if event.ticket_id == ticket_id),
                key=lambda event: event.occurred_at,
            )
        )
        screenshots = (self.screenshots[ticket_id],) if ticket_id in self.screenshots else ()
        return FeedbackAdminDetail(
            ticket,
            screenshots,
            tuple(self.rounds[ticket_id]),
            tuple(self.replies[ticket_id]),
            events,
            tuple(self.internal_notes[ticket_id]),
        )

    async def add_internal_note(
        self, note: FeedbackInternalNote, idempotency_key: str
    ) -> FeedbackInternalNote:
        # 功能: 保存仅管理员可见的反馈备注并保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     note: 待保存的反馈内部备注领域对象.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 已保存的内部备注.
        async with self._lock:
            if note.ticket_id not in self.tickets:
                raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
            identity = (note.ticket_id, note.admin_id, idempotency_key)
            replay = self._note_keys.get(identity)
            if replay is not None:
                return replay
            self.internal_notes[note.ticket_id].append(note)
            self.timeline.append(
                FeedbackTimelineEvent(
                    note.ticket_id,
                    "INTERNAL_NOTE_ADDED",
                    "ADMIN",
                    note.admin_id,
                    note.created_at,
                    visibility="ADMIN",
                )
            )
            self._note_keys[identity] = note
            return note


def _database_datetime(value: datetime | None) -> datetime | None:
    # 功能: 将带时区时间转换为数据库保存的无时区 UTC 时间.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_datetime(value: datetime | None) -> datetime | None:
    # 功能: 把数据库时间规范为带时区 UTC 时间.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 保留为空.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SQLAlchemyFeedbackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化反馈工单对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def create(
        self,
        ticket: FeedbackTicket,
        screenshots: Sequence[str],
        idempotency_key: str,
    ) -> FeedbackTicket:
        # 功能: 校验并创建反馈工单,保存截图和创建时间线,同时保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     screenshots: 反馈关联的截图 OSS 对象键序列,每次提交最多一张.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 当前或变更后的反馈工单.
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
        # 功能: 锁定反馈工单并执行状态变更及关联副作用,记录幂等结果.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     command: 反馈状态变更命令名称,纳入幂等隔离.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     actor_type: 执行操作的主体类型,例如 ADMIN 或 USER.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     mutation: 对锁定反馈执行状态变更并生成时间线,通知等副作用的回调.
        # 返回: 当前或变更后的反馈工单.
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
            if effects.screenshot_object_key is not None:
                existing_image = await session.scalar(
                    text("SELECT id FROM feedback_screenshot WHERE ticket_id=:ticket LIMIT 1"),
                    {"ticket": row["id"]},
                )
                if existing_image is not None:
                    raise AppError("FEEDBACK_SCREENSHOT_LIMIT", "每条反馈最多上传1张截图", 422)
                await session.execute(
                    text(
                        "INSERT INTO feedback_screenshot (ticket_id,object_key,security_status) "
                        "VALUES (:ticket,:key,'PENDING')"
                    ),
                    {"ticket": row["id"], "key": effects.screenshot_object_key},
                )
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
                session,
                row["id"],
                effects.event_type,
                actor_type,
                actor_id,
                now,
                payload=effects.timeline_payload,
                response_deadline=_utc_datetime(row["deadline_at"]),
            )
            if effects.round_request_text is not None:
                await session.execute(
                    text(
                        "INSERT INTO feedback_round "
                        "(ticket_id, round_number, request_text, paused_at) "
                        "VALUES (:ticket_id, :round_number, :request_text, :paused_at)"
                    ),
                    {
                        "ticket_id": row["id"],
                        "round_number": ticket.supplement_rounds,
                        "request_text": effects.round_request_text,
                        "paused_at": _database_datetime(now),
                    },
                )
            if effects.round_supplement_text is not None:
                await session.execute(
                    text(
                        "UPDATE feedback_round SET supplement_text = :supplement_text, "
                        "supplied_at = :supplied_at WHERE ticket_id = :ticket_id "
                        "AND round_number = :round_number"
                    ),
                    {
                        "ticket_id": row["id"],
                        "round_number": ticket.supplement_rounds,
                        "supplement_text": effects.round_supplement_text,
                        "supplied_at": _database_datetime(now),
                    },
                )
            if effects.reply_template is not None:
                await session.execute(
                    text(
                        "INSERT INTO feedback_reply "
                        "(ticket_id, template, note, admin_id, idempotency_key, sent_at) "
                        "VALUES (:ticket_id, :template, :note, :admin_id, :key, :sent_at)"
                    ),
                    {
                        "ticket_id": row["id"],
                        "template": effects.reply_template,
                        "note": effects.reply_note,
                        "admin_id": actor_id,
                        "key": idempotency_key,
                        "sent_at": _database_datetime(now),
                    },
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
        # 功能: 读取指定反馈工单记录,不存在时返回 None.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 匹配的反馈工单;不存在时为 None.
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

    async def list_admin(
        self,
        filters: dict[str, str],
        page: int,
        page_size: int,
        now: datetime,
    ) -> FeedbackAdminPage:
        # 功能: 按条件分页查询管理端反馈列表及总数.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 反馈管理列表项及分页统计.
        _validate_admin_query(filters, page, page_size)
        conditions: list[str] = []
        params: dict[str, object] = {
            "limit": page_size,
            "offset": (page - 1) * page_size,
            "now": _database_datetime(now),
            "due_soon": _database_datetime(now + timedelta(hours=12)),
        }
        if status := filters.get("status"):
            conditions.append("ft.status = :status")
            params["status"] = status
        if category := filters.get("category"):
            conditions.append("ft.category = :category")
            params["category"] = category
        if keyword := filters.get("keyword"):
            conditions.append("(ft.public_id = :keyword OR u.public_id = :keyword)")
            params["keyword"] = keyword
        sla_condition = _sql_sla_condition(filters.get("sla"))
        if sla_condition is not None:
            conditions.append(sla_condition)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sla_case = (
            "CASE "
            "WHEN ft.status = 'NEED_MORE' THEN 'PAUSED' "
            "WHEN ft.status IN ('RESOLVED','CLOSED_INSUFFICIENT') THEN 'COMPLETED' "
            "WHEN ft.deadline_at < :now THEN 'OVERDUE' "
            "WHEN ft.deadline_at <= :due_soon THEN 'DUE_SOON' "
            "ELSE 'ON_TRACK' END"
        )
        base = " FROM feedback_ticket ft JOIN user_account u ON u.id = ft.user_id"
        async with self._session_factory() as session:
            total = await session.scalar(text(f"SELECT COUNT(*){base}{where}"), params)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT ft.public_id, u.public_id AS user_public_id, ft.category, "
                            "ft.description, ft.source, ft.status, ft.deadline_at, "
                            "ft.supplement_rounds, "
                            "COALESCE((SELECT CASE WHEN fs.deleted_at IS NOT NULL THEN "
                            "'DELETED' ELSE fs.security_status END FROM feedback_screenshot "
                            "fs WHERE fs.ticket_id=ft.id LIMIT 1),'NONE') AS "
                            "screenshot_status, "
                            "(SELECT MAX(fr.supplied_at) FROM feedback_round fr WHERE "
                            "fr.ticket_id=ft.id) AS supplied_at, "
                            f"ft.created_at, ft.updated_at, {sla_case} AS sla_state"
                            f"{base}{where} ORDER BY "
                            "(ft.status IN ('PENDING','PROCESSING','USER_SUPPLIED') "
                            "AND ft.deadline_at < :now) DESC, "
                            "(ft.status='USER_SUPPLIED') DESC, ft.updated_at DESC, ft.id DESC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        items = tuple(
            FeedbackAdminListItem(
                id=row["public_id"],
                user_id=row["user_public_id"],
                category=row["category"],
                description=row["description"],
                status=row["status"],
                deadline_at=_utc_datetime(row["deadline_at"]),
                sla_state=row["sla_state"],
                supplement_rounds=row["supplement_rounds"],
                created_at=_required_utc(row["created_at"]),
                updated_at=_required_utc(row["updated_at"]),
                source=json.loads(row["source"])
                if isinstance(row["source"], str)
                else row["source"],
                screenshot_status=row["screenshot_status"],
                supplied_at=_utc_datetime(row["supplied_at"]),
            )
            for row in rows
        )
        return FeedbackAdminPage(items, page, page_size, int(total or 0))

    async def get_admin(self, ticket_id: str) -> FeedbackAdminDetail | None:
        # 功能: 查询管理端反馈详情,回复,时间线和内部备注.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 管理端反馈详情;不存在时为 None.
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
            if row is None:
                return None
            screenshots = (
                (
                    await session.execute(
                        text(
                            "SELECT fs.object_key, fs.security_status, fs.delete_after, "
                            "fs.deleted_at FROM feedback_screenshot fs "
                            "JOIN feedback_ticket ft ON ft.id = fs.ticket_id "
                            "WHERE ft.public_id = :public_id ORDER BY fs.id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .all()
            )
            rounds = (
                (
                    await session.execute(
                        text(
                            "SELECT fr.round_number, fr.request_text, fr.supplement_text, "
                            "fr.paused_at, fr.supplied_at FROM feedback_round fr "
                            "JOIN feedback_ticket ft ON ft.id = fr.ticket_id "
                            "WHERE ft.public_id = :public_id ORDER BY fr.round_number, fr.id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .all()
            )
            replies = (
                (
                    await session.execute(
                        text(
                            "SELECT fr.template, fr.note, fr.admin_id, fr.sent_at "
                            "FROM feedback_reply fr "
                            "JOIN feedback_ticket ft ON ft.id = fr.ticket_id "
                            "WHERE ft.public_id = :public_id ORDER BY fr.sent_at, fr.id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .all()
            )
            timeline = (
                (
                    await session.execute(
                        text(
                            "SELECT tl.event_type, tl.actor_type, tl.actor_id, tl.visibility, "
                            "tl.payload, tl.occurred_at FROM feedback_timeline tl "
                            "JOIN feedback_ticket ft ON ft.id = tl.ticket_id "
                            "WHERE ft.public_id = :public_id ORDER BY tl.occurred_at, tl.id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .all()
            )
            notes = (
                (
                    await session.execute(
                        text(
                            "SELECT n.public_id, n.admin_id, n.content, n.created_at "
                            "FROM feedback_internal_note n "
                            "JOIN feedback_ticket ft ON ft.id = n.ticket_id "
                            "WHERE ft.public_id = :public_id ORDER BY n.created_at, n.id"
                        ),
                        {"public_id": ticket_id},
                    )
                )
                .mappings()
                .all()
            )
        return FeedbackAdminDetail(
            ticket=self._from_row(row),
            screenshots=tuple(
                FeedbackScreenshot(
                    ticket_id,
                    item["object_key"],
                    item["security_status"],
                    _utc_datetime(item["delete_after"]),
                    _utc_datetime(item["deleted_at"]),
                )
                for item in screenshots
            ),
            rounds=tuple(
                FeedbackRound(
                    ticket_id,
                    item["round_number"],
                    item["request_text"],
                    item["supplement_text"],
                    _utc_datetime(item["paused_at"]),
                    _utc_datetime(item["supplied_at"]),
                )
                for item in rounds
            ),
            replies=tuple(
                FeedbackReply(
                    ticket_id,
                    item["template"],
                    item["note"],
                    item["admin_id"],
                    _required_utc(item["sent_at"]),
                )
                for item in replies
            ),
            timeline=tuple(
                FeedbackTimelineEvent(
                    ticket_id,
                    item["event_type"],
                    item["actor_type"],
                    item["actor_id"],
                    _required_utc(item["occurred_at"]),
                    item["visibility"],
                    _json_object(item["payload"]),
                )
                for item in timeline
            ),
            internal_notes=tuple(
                FeedbackInternalNote(
                    item["public_id"],
                    ticket_id,
                    item["admin_id"],
                    item["content"],
                    _required_utc(item["created_at"]),
                )
                for item in notes
            ),
        )

    async def add_internal_note(
        self, note: FeedbackInternalNote, idempotency_key: str
    ) -> FeedbackInternalNote:
        # 功能: 保存仅管理员可见的反馈备注并保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     note: 待保存的反馈内部备注领域对象.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 已保存的内部备注.
        async with self._session_factory() as session, session.begin():
            ticket_row = (
                await session.execute(
                    text("SELECT id FROM feedback_ticket WHERE public_id = :public_id FOR UPDATE"),
                    {"public_id": note.ticket_id},
                )
            ).first()
            if ticket_row is None:
                raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)
            ticket_internal_id = int(ticket_row.id)
            replay = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id, content, created_at FROM feedback_internal_note "
                            "WHERE ticket_id = :ticket_id AND admin_id = :admin_id "
                            "AND idempotency_key = :idempotency_key"
                        ),
                        {
                            "ticket_id": ticket_internal_id,
                            "admin_id": note.admin_id,
                            "idempotency_key": idempotency_key,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if replay is not None:
                return FeedbackInternalNote(
                    replay["public_id"],
                    note.ticket_id,
                    note.admin_id,
                    replay["content"],
                    _required_utc(replay["created_at"]),
                )
            await session.execute(
                text(
                    "INSERT INTO feedback_internal_note "
                    "(public_id, ticket_id, admin_id, content, idempotency_key, created_at) "
                    "VALUES (:public_id, :ticket_id, :admin_id, :content, :key, :created_at)"
                ),
                {
                    "public_id": note.id,
                    "ticket_id": ticket_internal_id,
                    "admin_id": note.admin_id,
                    "content": note.content,
                    "key": idempotency_key,
                    "created_at": _database_datetime(note.created_at),
                },
            )
            await self._insert_timeline(
                session,
                ticket_internal_id,
                "INTERNAL_NOTE_ADDED",
                "ADMIN",
                note.admin_id,
                note.created_at,
                visibility="ADMIN",
            )
            return note

    @staticmethod
    async def _lock_user(session: AsyncSession, public_id: str) -> int:
        # 功能: 锁定用户记录并取得内部主键以串行化反馈写入.
        # 参数:
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     public_id: 对外公开的业务标识,用于解析数据库内部主键.
        # 返回: 已锁定用户记录的数据库数值主键;用户不存在时抛出业务错误.
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
        # 功能: 按用户与创建幂等键加载已有反馈工单.
        # 参数:
        #     cls: 当前类,用于创建实例或调用类级辅助方法.
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     user_id: 用户数据库数值主键;允许 None 时表示无关联用户.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 匹配的反馈工单;不存在时为 None.
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
        *,
        visibility: str = "BOTH",
        payload: dict[str, object] | None = None,
        response_deadline: datetime | None = None,
    ) -> None:
        # 功能: 写入反馈事件的可见范围,载荷及响应截止信息.
        # 参数:
        #     session: 当前 SQLAlchemy 异步数据库会话,在调用方事务内执行读写.
        #     ticket_id: 反馈工单数据库数值主键.
        #     event_type: 业务事件类型,必须符合该事件的维度及载荷约束.
        #     actor_type: 执行操作的主体类型,例如 ADMIN 或 USER.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     visibility: 反馈时间线可见范围,例如 BOTH 或 ADMIN.
        #     payload: 反馈时间线事件附带的可见业务内容.
        #     response_deadline: 反馈首次响应截止时间;None 时无对应 SLA 截止值.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await session.execute(
            text(
                "INSERT INTO feedback_timeline "
                "(ticket_id, event_type, actor_type, actor_id, visibility, payload, occurred_at) "
                "VALUES (:ticket_id, :event_type, :actor_type, :actor_id, "
                ":visibility, :payload, :now)"
            ),
            {
                "ticket_id": ticket_id,
                "event_type": event_type,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "visibility": visibility,
                "payload": json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                "now": _database_datetime(now),
            },
        )

        if visibility == "BOTH":
            await feedback_event(session, ticket_id, event_type, actor_type, now, response_deadline)

    @staticmethod
    def _from_row(row: RowMapping) -> FeedbackTicket:
        # 功能: 将数据库记录转换为反馈工单领域对象.
        # 参数:
        #     row: 查询得到的反馈工单数据库记录.
        # 返回: 当前或变更后的反馈工单.
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


_ADMIN_FILTERS = {"status", "category", "keyword", "sla"}
_FEEDBACK_STATUSES = {
    "PENDING",
    "PROCESSING",
    "NEED_MORE",
    "USER_SUPPLIED",
    "RESOLVED",
    "CLOSED_INSUFFICIENT",
}
_FEEDBACK_CATEGORIES = {"CONTENT", "PRONUNCIATION", "DISPLAY", "FUNCTION"}
_SLA_STATES = {"PAUSED", "OVERDUE", "DUE_SOON", "ON_TRACK", "COMPLETED"}


def _validate_admin_query(filters: dict[str, str], page: int, page_size: int) -> None:
    # 功能: 校验反馈列表筛选字段及分页边界.
    # 参数:
    #     filters: 业务列表的筛选条件映射,空映射表示不限.
    #     page: 分页页码,从 1 开始,默认第 1 页.
    #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
    # 返回: 无返回值;正常完成表示本次操作成功.
    if set(filters) - _ADMIN_FILTERS:
        raise AppError("FEEDBACK_FILTER_INVALID", "反馈筛选条件不正确", 422)
    if filters.get("status") not in _FEEDBACK_STATUSES | {None}:
        raise AppError("FEEDBACK_FILTER_INVALID", "反馈状态筛选不正确", 422)
    if filters.get("category") not in _FEEDBACK_CATEGORIES | {None}:
        raise AppError("FEEDBACK_FILTER_INVALID", "反馈分类筛选不正确", 422)
    if filters.get("sla") not in _SLA_STATES | {None, "URGENT"}:
        raise AppError("FEEDBACK_FILTER_INVALID", "反馈 SLA 筛选不正确", 422)
    if page < 1 or not 1 <= page_size <= 100:
        raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)


def _sla_state(ticket: FeedbackTicket, now: datetime) -> str:
    # 功能: 根据反馈状态和截止时间计算 SLA 展示状态.
    # 参数:
    #     ticket: 正在查询,持久化或变更的反馈工单.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 反馈 SLA 状态:PAUSED,COMPLETED,OVERDUE,DUE_SOON 或 ON_TRACK.
    if ticket.status == "NEED_MORE":
        return "PAUSED"
    if ticket.status in {"RESOLVED", "CLOSED_INSUFFICIENT"}:
        return "COMPLETED"
    if ticket.deadline_at is not None and ticket.deadline_at < now:
        return "OVERDUE"
    if ticket.deadline_at is not None and ticket.deadline_at <= now + timedelta(hours=12):
        return "DUE_SOON"
    return "ON_TRACK"


def _matches_admin_filters(ticket: FeedbackTicket, filters: dict[str, str], now: datetime) -> bool:
    # 功能: 检查反馈是否匹配管理员列表筛选条件.
    # 参数:
    #     ticket: 正在查询,持久化或变更的反馈工单.
    #     filters: 业务列表的筛选条件映射,空映射表示不限.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 反馈符合全部筛选条件时为 True.
    keyword = filters.get("keyword")
    return (
        (filters.get("status") in {None, ticket.status})
        and (filters.get("category") in {None, ticket.category})
        and (keyword is None or keyword in {ticket.id, ticket.user_id})
        and (
            filters.get("sla") in {None, _sla_state(ticket, now)}
            or (
                filters.get("sla") == "URGENT"
                and (
                    ticket.status == "USER_SUPPLIED"
                    or _sla_state(ticket, now) in {"OVERDUE", "DUE_SOON"}
                )
            )
        )
    )


def _list_item(ticket: FeedbackTicket, now: datetime) -> FeedbackAdminListItem:
    # 功能: 将反馈工单和当前 SLA 状态组合为管理列表项.
    # 参数:
    #     ticket: 正在查询,持久化或变更的反馈工单.
    #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
    # 返回: 反馈工单与 SLA 展示状态组成的管理列表项.
    return FeedbackAdminListItem(
        id=ticket.id,
        user_id=ticket.user_id,
        category=ticket.category,
        description=ticket.description,
        status=ticket.status,
        deadline_at=ticket.deadline_at,
        sla_state=_sla_state(ticket, now),
        supplement_rounds=ticket.supplement_rounds,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        source=ticket.source,
    )


def _sql_sla_condition(sla: str | None) -> str | None:
    # 功能: 把 SLA 筛选模式转换为受控 SQL 条件.
    # 参数:
    #     sla: 反馈响应时限状态筛选条件,例如 OVERDUE 或 DUE_SOON.
    # 返回: 受控 SLA SQL 条件;未指定或无匹配模式时为 None.
    if sla == "URGENT":
        return (
            "(ft.status='USER_SUPPLIED' OR (ft.status IN "
            "('PENDING','PROCESSING') AND ft.deadline_at<=:due_soon))"
        )
    if sla == "PAUSED":
        return "ft.status = 'NEED_MORE'"
    if sla == "COMPLETED":
        return "ft.status IN ('RESOLVED','CLOSED_INSUFFICIENT')"
    active = "ft.status NOT IN ('NEED_MORE','RESOLVED','CLOSED_INSUFFICIENT')"
    if sla == "OVERDUE":
        return f"{active} AND ft.deadline_at < :now"
    if sla == "DUE_SOON":
        return f"{active} AND ft.deadline_at >= :now AND ft.deadline_at <= :due_soon"
    if sla == "ON_TRACK":
        return f"{active} AND ft.deadline_at > :due_soon"
    return None


def _required_utc(value: datetime | None) -> datetime:
    # 功能: 转换必填数据库时间为 UTC,空值时拒绝解析.
    # 参数:
    #     value: 待规范化时区或转换业务日期的时间;None 会触发必填时间校验错误.
    # 返回: 规范化或计算后的带时区日期时间.
    result = _utc_datetime(value)
    if result is None:
        raise RuntimeError("Feedback timestamp cannot be null")
    return result


def _json_object(value: object) -> dict[str, object]:
    # 功能: 将数据库 JSON 内容解析为业务字典.
    # 参数:
    #     value: 数据库或业务存储中的 JSON 文本或已解码对象.
    # 返回: 解析后的反馈 JSON 字典;无有效对象结构时返回空字典.
    parsed = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, dict) else {}
