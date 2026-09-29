import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.testclient import TestClient

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.runtime import Runtime
from juya_admin_api.integrations.miniapp_api.client import (
    ContactCorrection,
    ContactCorrectionPage,
    ContactProjection,
    CorrectionDecision,
)
from juya_admin_api.main import create_app
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError, install_error_handlers


class FakeRuntime:
    def __init__(self) -> None:
        router = APIRouter(prefix="/api/v1/admin")

        @router.get("/runtime-smoke")
        async def runtime_smoke() -> dict[str, bool]:
            return {"assembled": True}

        self.routers = (router,)
        self.readiness_probe = self.ready
        self.closed = False

    async def ready(self) -> Mapping[str, bool]:
        return {"mysql": True, "redis": True, "schema": True, "configuration": True}

    async def close(self) -> None:
        self.closed = True


def test_runtime_routes_health_and_lifecycle_are_assembled() -> None:
    runtime = FakeRuntime()
    app = create_app(Settings(), runtime=cast(Runtime, cast(Any, runtime)))

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/admin/runtime-smoke").json() == {"assembled": True}

    assert runtime.closed is True


def test_container_runs_as_non_root_and_keeps_migration_separate() -> None:
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint_path = root / "scripts" / "entrypoint.sh"
    entrypoint = entrypoint_path.read_text(encoding="utf-8")
    aliyun_pipeline = (root / ".aliyun-ci.yml").read_text(encoding="utf-8")
    ecs_compose = (root / "deploy" / "docker-compose.ecs.yml").read_text(encoding="utf-8")

    assert b"\r\n" not in entrypoint_path.read_bytes()
    assert any(
        dependency.startswith("httpx") for dependency in pyproject["project"]["dependencies"]
    )
    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "migrate)" in entrypoint
    assert "alembic upgrade head" in entrypoint
    assert "admin-api)" in entrypoint
    assert "--schedule /tmp/celerybeat-schedule" in entrypoint
    assert "component: VMDeploy" in aliyun_pipeline
    assert "step: ACRDockerBuild" in aliyun_pipeline
    assert "aliyun-kubectl" not in aliyun_pipeline
    assert "read_only: true" in ecs_compose
    assert "JUYA_PROCESS_ROLE: admin-beat" in ecs_compose


def test_local_compose_seeds_admin_after_migration_before_api() -> None:
    """验证本地管理员初始化角色与 Compose 依赖顺序

    Returns:
        None
    """
    root = Path(__file__).parents[2]
    shell_entrypoint = (root / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    powershell_entrypoint = (root / "scripts" / "entrypoint.ps1").read_text(encoding="utf-8")
    local_compose = (root / "docker-compose.dev.yml").read_text(encoding="utf-8")
    ecs_compose = (root / "deploy" / "docker-compose.ecs.yml").read_text(encoding="utf-8")

    assert "seed-local-admin)" in shell_entrypoint
    assert "python -m juya_admin_api.local_admin" in shell_entrypoint
    assert "'seed-local-admin'" in powershell_entrypoint
    assert "python '-m' 'juya_admin_api.local_admin'" in powershell_entrypoint

    assert "  seed-local-admin:" in local_compose
    seed_block = local_compose.split("  seed-local-admin:", maxsplit=1)[1].split(
        "\n  admin-api:", maxsplit=1
    )[0]
    admin_api_block = local_compose.split("  admin-api:", maxsplit=1)[1].split(
        "\n  admin-worker-content:", maxsplit=1
    )[0]
    assert "JUYA_PROCESS_ROLE: seed-local-admin" in seed_block
    assert "migrate:\n        condition: service_completed_successfully" in seed_block
    assert "JUYA_LOCAL_ADMIN_USERNAME: ${JUYA_LOCAL_ADMIN_USERNAME:-admin}" in seed_block
    assert "JUYA_LOCAL_ADMIN_PASSWORD: ${JUYA_LOCAL_ADMIN_PASSWORD:-JuyaLocal@2026}" in seed_block
    assert "JUYA_LOCAL_ADMIN_TOTP_SECRET" not in seed_block
    assert "seed-local-admin:\n        condition: service_completed_successfully" in admin_api_block
    assert "seed-local-admin" not in ecs_compose


def test_contact_admin_routes_enforce_auth_csrf_idempotency_and_no_store() -> None:
    from juya_admin_api.modules.contacts.router import create_contact_router
    from juya_admin_api.modules.contacts.service import ContactAdminService

    now = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)
    session = SessionRecord(
        "session-1",
        7,
        "token-hash",
        "csrf-hash",
        "test",
        now + timedelta(hours=1),
        now,
    )

    class Client:
        async def list_contact_corrections(
            self, status: str | None, page: int, page_size: int, admin_id: str
        ) -> ContactCorrectionPage:
            del status, admin_id
            correction = ContactCorrection(
                "correction-1",
                "user-1",
                "JY000000000001",
                "学习者",
                "wx-private",
                "微信号需要更正",
                "PENDING",
                now,
                None,
                (),
            )
            return ContactCorrectionPage((correction,), 1, page, page_size)

        async def get_contact_correction(
            self, correction_id: str, admin_id: str
        ) -> ContactCorrection:
            del correction_id, admin_id
            return (await self.list_contact_corrections(None, 1, 20, "admin")).items[0]

        async def update_contact_status(
            self, user_id: str, status: str, admin_id: str
        ) -> ContactProjection:
            return ContactProjection(user_id, "wx-private", status, False, None, admin_id, now)

        async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
            return ContactProjection(user_id, "wx-private", "PENDING", False, now, admin_id, now)

        async def decide_contact_correction(
            self,
            correction_id: str,
            decision: str,
            admin_id: str,
            idempotency_key: str,
        ) -> CorrectionDecision:
            del admin_id, idempotency_key
            return CorrectionDecision(correction_id, decision, now)

    class AuditRepository:
        def __init__(self) -> None:
            self.events: list[AuditEvent] = []

        async def append(self, event: AuditEvent) -> None:
            self.events.append(event)

        async def list_recent(self, limit: int) -> list[AuditEvent]:
            return self.events[-limit:]

    async def current_admin(x_test_auth: str | None = Header(default=None)) -> SessionRecord:
        if x_test_auth != "ok":
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return session

    async def current_admin_write(
        x_test_auth: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        authenticated = await current_admin(x_test_auth)
        if x_csrf_token != "csrf-ok":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF校验失败", 403)
        return authenticated

    audit_repository = AuditRepository()
    service = ContactAdminService(Client(), AuditService(audit_repository))
    app = FastAPI()
    install_error_handlers(app)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = "request-e2e"
        return await call_next(request)

    app.include_router(
        create_contact_router(
            service,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
            clock=lambda: now,
        )
    )

    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/admin/contact-corrections")
        listed = client.get("/api/v1/admin/contact-corrections", headers={"X-Test-Auth": "ok"})
        missing_csrf = client.post(
            "/api/v1/admin/users/user-1/commands/contact-status",
            json={"status": "CONTACTED"},
            headers={"X-Test-Auth": "ok"},
        )
        missing_idempotency = client.post(
            "/api/v1/admin/contact-corrections/correction-1/commands/approve",
            headers={"X-Test-Auth": "ok", "X-CSRF-Token": "csrf-ok"},
        )
        copied = client.post(
            "/api/v1/admin/users/user-1/contact-copy-events",
            headers={"X-Test-Auth": "ok", "X-CSRF-Token": "csrf-ok"},
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()["items"][0]["wechat_id"] == "wx-private"
    assert missing_csrf.status_code == 403
    assert missing_idempotency.status_code == 422
    assert copied.status_code == 204
    assert copied.headers["Cache-Control"] == "no-store"
    assert audit_repository.events[-1].action == "contact.copy"
