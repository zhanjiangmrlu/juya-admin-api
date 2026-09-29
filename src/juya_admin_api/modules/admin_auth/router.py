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


def create_admin_security_router(
    auth_service: AdminAuthService,
    config_service: SystemConfigService,
    *,
    audit_service: AuditService | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-security"])

    async def authenticated_session(
        session_token: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> SessionRecord:
        if session_token is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return await auth_service.authenticate_session(session_token, clock())

    async def csrf_session(
        session: Annotated[SessionRecord, Depends(authenticated_session)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> SessionRecord:
        auth_service.verify_csrf(session, csrf_token)
        return session

    @router.post("/session")
    async def create_password_session(
        payload: PasswordLoginRequest, request: Request, response: Response
    ) -> dict[str, object]:
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
        csrf_token = await auth_service.rotate_csrf(session)
        return {"csrf_token": csrf_token, "expires_at": session.expires_at}

    @router.post("/session/logout", status_code=204)
    async def logout(
        response: Response,
        session: Annotated[SessionRecord, Depends(csrf_session)],
    ) -> None:
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
        events = [] if audit_service is None else await audit_service.list_recent(limit)
        return {
            "items": [
                {
                    "actor_public_id": event.actor_public_id,
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
