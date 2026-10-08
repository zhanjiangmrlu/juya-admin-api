from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from inspect import isawaitable

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
    # 匿名函数: 在未注入配置读取器时提供默认反馈 SLA.
    # 参数: 无.
    # 返回: 默认响应时限 48 小时.
    def __init__(
        self,
        repository: FeedbackRepository,
        *,
        sla_hours_provider: Callable[[], int | Awaitable[int]] = lambda: 48,
    ) -> None:
        # 功能: 初始化反馈工单对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供反馈工单持久化和查询能力的仓储.
        #     sla_hours_provider: 返回反馈响应时限小时数的同步或异步回调.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository
        self._sla_hours_provider = sla_hours_provider

    async def get(self, ticket_id: str) -> FeedbackTicket:
        # 功能: 读取指定反馈工单记录,不存在时抛出业务错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 当前或变更后的反馈工单.
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
        # 功能: 按条件分页查询管理端反馈列表及总数.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 反馈管理列表项及分页统计.
        return await self._repository.list_admin(filters, page, page_size, now)

    async def get_admin(self, ticket_id: str) -> FeedbackAdminDetail:
        # 功能: 查询管理端反馈详情,回复,时间线和内部备注.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        # 返回: 管理端反馈详情,包括时间线,回复和内部备注.
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
        # 功能: 保存仅管理员可见的反馈备注并保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     content: 仅管理员可见的反馈备注正文,不能为空且最多 200 字.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 已保存的内部备注.
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
        # 功能: 校验并创建反馈工单,保存截图和创建时间线,同时保证幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     category: 反馈分类,例如 CONTENT,PRONUNCIATION,DISPLAY 或 FUNCTION.
        #     description: 用户填写的反馈说明,不能为空且最多 300 字.
        #     source: 反馈来源上下文,包括场景,词条或页面定位信息.
        #     screenshots: 反馈关联的截图 OSS 对象键序列,每次提交最多一张.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        if category not in _CATEGORIES:
            raise AppError("FEEDBACK_CATEGORY_INVALID", "反馈分类无效", 422)
        if not description.strip():
            raise AppError("FEEDBACK_DESCRIPTION_REQUIRED", "请填写反馈说明", 422)
        if len(description) > 300:
            raise AppError("FEEDBACK_DESCRIPTION_TOO_LONG", "反馈说明最多300字", 422)
        if len(screenshots) > 1:
            raise AppError("FEEDBACK_SCREENSHOT_LIMIT", "每条反馈最多一张截图", 422)
        sla_hours = await self._sla_hours()
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
        # 功能: 将待处理或用户已补充的反馈切换为处理中.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 校验反馈状态并切换为处理中,生成时间线副作用.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
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
        # 功能: 要求用户补充反馈信息并暂停 SLA 计时.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     request_text: 管理员要求用户补充的信息说明,不能为空且最多 200 字.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        if not request_text.strip() or len(request_text) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "补充要求最多200字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 检查补充轮次并暂停 SLA,生成补充要求和用户通知.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
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
        *,
        screenshots: Sequence[str] = (),
    ) -> FeedbackTicket:
        # 功能: 提交用户补充信息并重新启动反馈响应计时.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     supplement: 用户补充的反馈说明,不能为空且最多 300 字.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     screenshots: 反馈关联的截图 OSS 对象键序列,每次提交最多一张.
        # 返回: 当前或变更后的反馈工单.
        if not supplement.strip() or len(supplement) > 300:
            raise AppError("FEEDBACK_SUPPLEMENT_INVALID", "补充说明最多300字", 422)
        if len(screenshots) > 1:
            raise AppError("FEEDBACK_SCREENSHOT_LIMIT", "每条反馈最多上传1张截图", 422)
        if any(not key.startswith(f"feedback/{user_id}/") for key in screenshots):
            raise AppError("FEEDBACK_SCREENSHOT_INVALID", "反馈截图无效", 422)

        sla_hours = await self._sla_hours()

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 校验反馈归属并重启 SLA,保存用户补充内容和截图关联.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
            self._require_owner(ticket, user_id)
            self._require_status(ticket, {"NEED_MORE"})
            ticket.sla_hours = sla_hours
            ticket.deadline_at = now + timedelta(hours=sla_hours)
            ticket.sla_remaining_seconds = None
            ticket.status = "USER_SUPPLIED"
            return CommandEffects(
                "USER_SUPPLIED",
                timeline_payload={"supplement_text": supplement},
                round_supplement_text=supplement,
                screenshot_object_key=screenshots[0] if screenshots else None,
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
        # 功能: 通过处理模板解决反馈并生成用户通知.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     template: 反馈处理结果模板,只接受 RESOLVED 或 TEMPORARILY_UNAVAILABLE.
        #     note: 解决反馈时的可选补充说明,最多 200 字.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        if note is not None and len(note) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "回复补充说明最多200字", 422)
        if template not in {"RESOLVED", "TEMPORARILY_UNAVAILABLE"}:
            raise AppError("FEEDBACK_TEMPLATE_INVALID", "反馈回复模板无效", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 更新反馈解决状态并生成回复和用户通知.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
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
        # 功能: 在解决后七天窗口内重开一次反馈并恢复 SLA 计时.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        if not reason.strip() or len(reason) > 300:
            raise AppError("FEEDBACK_REOPEN_REASON_INVALID", "重开原因最多300字", 422)

        sla_hours = await self._sla_hours()

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 校验反馈归属,重开次数及七天窗口后重新开始处理.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
            self._require_owner(ticket, user_id)
            self._require_status(ticket, {"RESOLVED"})
            if ticket.reopen_count >= 1:
                raise AppError("FEEDBACK_REOPEN_LIMIT", "反馈最多重开一次", 409)
            if ticket.resolved_at is None or now > ticket.resolved_at + timedelta(days=7):
                raise AppError("FEEDBACK_REOPEN_WINDOW_EXPIRED", "反馈重开期限已过", 409)
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
        # 功能: 以信息不足原因关闭反馈并设置截图保留期.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     ticket_id: 反馈工单公开标识.
        #     reason: 业务状态变更的原因说明,供校验和审计记录.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 当前或变更后的反馈工单.
        if not reason.strip() or len(reason) > 200:
            raise AppError("FEEDBACK_REPLY_INVALID", "关闭原因最多200字", 422)

        def mutation(ticket: FeedbackTicket) -> CommandEffects:
            # 功能: 关闭信息不足的反馈并生成用户通知和截图保留截止时间.
            # 参数:
            #     ticket: 正在查询,持久化或变更的反馈工单.
            # 返回: 反馈状态变更产生的时间线,通知及回复副作用.
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

    async def _sla_hours(self) -> int:
        # 功能: 读取反馈响应时限并限制在 1 至 720 小时.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 限制在 1 至 720 范围内的反馈响应时限小时数.
        value = self._sla_hours_provider()
        if isawaitable(value):
            value = await value
        return max(1, min(int(value), 24 * 30))

    @staticmethod
    def _require_status(ticket: FeedbackTicket, allowed: set[str]) -> None:
        # 功能: 检查反馈当前状态是否允许执行命令.
        # 参数:
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     allowed: 允许执行当前命令的反馈状态集合.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if ticket.status not in allowed:
            raise AppError("FEEDBACK_STATE_CONFLICT", "反馈状态已变化", 409)

    @staticmethod
    def _require_owner(ticket: FeedbackTicket, user_id: str) -> None:
        # 功能: 检查反馈是否属于当前用户,隐藏其他用户的工单.
        # 参数:
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if ticket.user_id != user_id:
            raise AppError("FEEDBACK_NOT_FOUND", "反馈不存在", 404)

    @staticmethod
    def _message(
        ticket: FeedbackTicket, message_type: str, title: str, summary: str
    ) -> FeedbackOutboxMessage:
        # 功能: 为反馈状态变化创建 outbox 站内通知.
        # 参数:
        #     ticket: 正在查询,持久化或变更的反馈工单.
        #     message_type: 小程序站内通知的业务类型.
        #     title: 站内通知展示标题.
        #     summary: 站内通知展示的简短摘要.
        # 返回: 等待派发给小程序的反馈通知.
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
