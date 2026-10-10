from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactCorrectionPage,
    ContactProjection,
    CorrectionDecision,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

CONTACT_STATUSES = frozenset(
    {"NOT_PROVIDED", "PENDING", "CONTACTED", "UNREACHABLE", "DO_NOT_CONTACT"}
)


class ContactClient(Protocol):
    async def list_contact_corrections(
        self, status: str | None, page: int, page_size: int, admin_id: str
    ) -> ContactCorrectionPage:
        # 功能: 分页查询联系信息纠错申请.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     status: 联系人跟进状态或纠错处理状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 纠错申请列表及分页信息.
        ...

    async def get_contact_correction(self, correction_id: str, admin_id: str) -> ContactCorrection:
        # 功能: 获取指定联系信息纠错申请详情.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 联系信息纠错申请详情.
        ...

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection:
        # 功能: 更新用户联系信息的跟进状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     status: 联系人跟进状态或纠错处理状态.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 用户的联系信息投影.
        ...

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
        # 功能: 确认用户联系信息变更已经核实.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 用户的联系信息投影.
        ...

    async def decide_contact_correction(
        self,
        correction_id: str,
        decision: str,
        admin_id: str,
        idempotency_key: str,
    ) -> CorrectionDecision:
        # 功能: 提交联系信息纠错处理决定并保留幂等语义.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     decision: 纠错处理决定,例如接受或拒绝.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 纠错处理后的结果.
        ...


class ContactAdminService:
    def __init__(self, client: ContactClient, audit: AuditService) -> None:
        # 功能: 初始化用户联系信息对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     client: 联系人信息查询和纠错操作的内部服务客户端.
        #     audit: 记录管理员敏感操作的审计服务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._client = client
        self._audit = audit

    async def list_corrections(
        self,
        status: str | None,
        page: int,
        page_size: int,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactCorrectionPage:
        # 功能: 分页读取联系信息纠错申请并记录访问审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     status: 联系人跟进状态或纠错处理状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 纠错申请列表及分页信息.
        result = await self._client.list_contact_corrections(status, page, page_size, actor_id)
        await self._record(
            actor_id,
            "contact.view",
            "contact_correction_list",
            "list",
            request_id,
            now,
            after_summary={"count": len(result.items), "status": status or "ALL"},
        )
        return result

    async def get_correction(
        self,
        correction_id: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactCorrection:
        # 功能: 读取联系信息纠错详情并记录审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 联系信息纠错申请详情.
        result = await self._client.get_contact_correction(correction_id, actor_id)
        await self._record(
            actor_id,
            "contact.view",
            "contact_correction",
            correction_id,
            request_id,
            now,
            after_summary={"status": result.status, "user_id": result.user_id},
        )
        return result

    async def update_status(
        self,
        user_id: str,
        status: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactProjection:
        # 功能: 变更联系信息跟进状态并记录审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     status: 联系人跟进状态或纠错处理状态.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 用户的联系信息投影.
        if status not in CONTACT_STATUSES:
            raise AppError("CONTACT_STATUS_INVALID", "联系状态无效", 422)
        result = await self._client.update_contact_status(user_id, status, actor_id)
        await self._record(
            actor_id,
            "contact.status.update",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"status": result.contact_status},
        )
        return result

    async def verify_change(
        self,
        user_id: str,
        actor_id: str,
        request_id: str,
        now: datetime,
    ) -> ContactProjection:
        # 功能: 核实联系信息变更并记录审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 用户的联系信息投影.
        result = await self._client.verify_contact_change(user_id, actor_id)
        await self._record(
            actor_id,
            "contact.change.verify",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"change_pending": result.change_pending},
        )
        return result

    async def decide_correction(
        self,
        correction_id: str,
        decision: str,
        actor_id: str,
        idempotency_key: str,
        request_id: str,
        now: datetime,
    ) -> CorrectionDecision:
        # 功能: 执行联系信息纠错决定并记录审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     decision: 纠错处理决定,例如接受或拒绝.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 纠错处理后的结果.
        normalized = decision.upper()
        action_by_decision = {
            "APPROVED": "contact.correction.approve",
            "REJECTED": "contact.correction.reject",
        }
        if normalized not in action_by_decision:
            raise AppError("CORRECTION_DECISION_INVALID", "更正决定无效", 422)
        result = await self._client.decide_contact_correction(
            correction_id, normalized, actor_id, idempotency_key
        )
        await self._record(
            actor_id,
            action_by_decision[normalized],
            "contact_correction",
            correction_id,
            request_id,
            now,
            after_summary={"status": result.status},
        )
        return result

    async def audit_copy(self, user_id: str, actor_id: str, request_id: str, now: datetime) -> None:
        # 功能: 记录管理员复制用户联系信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._record(
            actor_id,
            "contact.copy",
            "user_contact",
            user_id,
            request_id,
            now,
            after_summary={"copied": True},
        )

    async def _record(
        self,
        actor_id: str,
        action: str,
        object_type: str,
        object_id: str,
        request_id: str,
        now: datetime,
        *,
        after_summary: dict[str, object],
    ) -> None:
        # 功能: 生成并写入联系人操作的审计事件.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     action: 写入审计日志的操作名称.
        #     object_type: 审计对象的业务类型.
        #     object_id: 审计操作对应的业务对象标识.
        #     request_id: 本次 HTTP 请求的关联标识,串联日志与审计.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     after_summary: 允许进入审计记录的操作后摘要.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._audit.record(
            AuditEvent(
                actor_public_id=actor_id,
                action=action,
                object_type=object_type,
                object_public_id=object_id,
                before_summary={},
                after_summary=after_summary,
                reason=None,
                request_id=request_id,
                occurred_at=now,
            )
        )
