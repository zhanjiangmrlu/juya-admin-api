from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.admin_auth.service import AdminAuthService
from juya_admin_api.modules.audit.service import AuditService
from juya_admin_api.modules.system_config.service import SystemConfigService
from juya_admin_api.shared.errors import AppError

ADMIN_SESSION_COOKIE = "juya_admin_session"


class PasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, object]
    expected_version: int = Field(ge=1)


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_admin_security_router(
    auth_service: AdminAuthService,
    config_service: SystemConfigService,
    *,
    audit_service: AuditService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建管理员登录,会话,系统配置和审计查询路由.
    # 参数:
    #     auth_service: 管理员密码登录,会话和 CSRF 校验服务.
    #     config_service: 读取和更新系统配置的服务.
    #     audit_service: 可选审计服务,未配置时跳过审计写入.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-security"])

    async def authenticated_session(
        session_token: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> SessionRecord:
        # 功能: 从 Cookie 认证管理员会话.
        # 参数:
        #     session_token: 浏览器 Cookie 中的管理员会话令牌明文.
        # 返回: 经过查询或认证的管理员会话记录.
        if session_token is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return await auth_service.authenticate_session(session_token, clock())

    async def csrf_session(
        session: Annotated[SessionRecord, Depends(authenticated_session)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> SessionRecord:
        # 功能: 校验当前管理员会话对应的 CSRF 令牌.
        # 参数:
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        #     csrf_token: 客户端提交的 CSRF 令牌明文,缺失或与会话不匹配时拒绝写入.
        # 返回: 经过查询或认证的管理员会话记录.
        auth_service.verify_csrf(session, csrf_token)
        return session

    @router.post("/session")
    async def create_password_session(
        payload: PasswordLoginRequest, request: Request, response: Response
    ) -> dict[str, object]:
        # 功能: 验证管理员密码,创建会话并设置安全 Cookie.
        # 参数:
        #     payload: 管理员登录名,密码和设备摘要.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        # 返回: 新会话的 csrf_token 明文和 expires_at 到期时间;会话令牌另写入 Cookie.
        session = await auth_service.login_with_password(
            payload.username,
            payload.password,
            request.headers.get("user-agent", "unknown"),
            clock(),
        )
        response.set_cookie(
            ADMIN_SESSION_COOKIE,
            session.token,
            max_age=8 * 60 * 60,
            path="/api/v1/admin",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return {"csrf_token": session.csrf_token, "expires_at": session.expires_at}

    @router.get("/session")
    async def get_session(
        session: Annotated[SessionRecord, Depends(authenticated_session)],
    ) -> dict[str, object]:
        # 功能: 返回管理员会话信息并轮换 CSRF 令牌.
        # 参数:
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 轮换后的 csrf_token 明文及当前会话 expires_at 到期时间.
        csrf_token = await auth_service.rotate_csrf(session)
        return {"csrf_token": csrf_token, "expires_at": session.expires_at}

    @router.post("/session/logout", status_code=204)
    async def logout(
        response: Response,
        session: Annotated[SessionRecord, Depends(csrf_session)],
    ) -> None:
        # 功能: 撤销当前管理员会话.
        # 参数:
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await auth_service.logout(session.id, clock())
        response.delete_cookie(
            ADMIN_SESSION_COOKIE,
            path="/api/v1/admin",
            secure=True,
            httponly=True,
            samesite="strict",
        )

    @router.get("/settings")
    async def list_settings(
        _session: Annotated[SessionRecord, Depends(authenticated_session)],
    ) -> dict[str, object]:
        # 功能: 返回后台系统配置列表.
        # 参数:
        #     _session: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: items 配置列表,各项包含 key,value 和 version.
        configs = await config_service.list()
        return {
            "items": [
                {"key": item.key, "value": item.value, "version": item.version} for item in configs
            ]
        }

    @router.patch("/settings/{key}")
    async def update_setting(
        key: str,
        payload: ConfigUpdateRequest,
        session: Annotated[SessionRecord, Depends(csrf_session)],
    ) -> dict[str, object]:
        # 功能: 按预期版本更新系统配置.
        # 参数:
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     payload: 待更新配置值及调用方读取到的版本号.
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        # 返回: 更新后的配置 key,value 和递增的 version.
        updated = await config_service.update(
            key,
            payload.value,
            payload.expected_version,
            str(session.admin_user_id),
        )
        return {"key": updated.key, "value": updated.value, "version": updated.version}

    @router.get("/audit-events")
    async def list_audit_events(
        _session: Annotated[SessionRecord, Depends(authenticated_session)],
        limit: int = 100,
    ) -> dict[str, object]:
        # 功能: 查询最近的管理员操作审计记录.
        # 参数:
        #     _session: 认证依赖注入的管理员会话,仅用于执行访问校验.
        #     limit: 最近记录的最大返回条数.
        # 返回: items 审计列表,含主体,动作,对象,前后摘要,原因,请求标识和发生时间.
        events = [] if audit_service is None else await audit_service.list_recent(limit)
        return {
            "items": [
                {
                    "actor_public_id": event.actor_public_id,
                    "actor_name": event.actor_name,
                    "action": event.action,
                    "object_type": event.object_type,
                    "object_public_id": event.object_public_id,
                    "before_summary": event.before_summary,
                    "after_summary": event.after_summary,
                    "reason": event.reason,
                    "request_id": event.request_id,
                    "occurred_at": event.occurred_at,
                }
                for event in events
            ]
        }

    return router
