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
    ContactProjectionResult,
    CorrectionDecision,
    LearningOverview,
)
from juya_admin_api.main import create_app
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError, install_error_handlers


class FakeRuntime:
    def __init__(self) -> None:
        # 功能:初始化 FakeRuntime 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeRuntime 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        router = APIRouter(prefix="/api/v1/admin")

        @router.get("/runtime-smoke")
        async def runtime_smoke() -> dict[str, bool]:
            # 功能:返回运行时已组装标志,供路由挂载检查。
            # 参数:无。
            # 返回:dict[str, bool],由本用例预设的数据或所组装的测试资源构成。
            return {"assembled": True}

        self.routers = (router,)
        self.readiness_probe = self.ready
        self.closed = False

    async def ready(self) -> Mapping[str, bool]:
        # 功能:返回测试指定的依赖就绪状态。
        # 参数:
        #     self: 当前 FakeRuntime 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:各依赖就绪状态的映射。
        return {"mysql": True, "redis": True, "schema": True, "configuration": True}

    async def close(self) -> None:
        # 功能:模拟资源关闭;记录或更新关闭状态供清理断言。
        # 参数:
        #     self: 当前 FakeRuntime 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.closed = True


def test_runtime_routes_health_and_lifecycle_are_assembled() -> None:
    # 功能:验证运行时组装健康检查和生命周期接口。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    runtime = FakeRuntime()
    app = create_app(Settings(), runtime=cast(Runtime, cast(Any, runtime)))

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/admin/runtime-smoke").json() == {"assembled": True}

    assert runtime.closed is True


def test_container_runs_as_non_root_and_keeps_migration_separate() -> None:
    # 功能:验证容器使用非 root 用户且迁移独立运行。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    assert "step: ArtifactUpload" in aliyun_pipeline
    assert "build-artifact.sh" in aliyun_pipeline
    assert "ecs-artifact-deploy.sh" in aliyun_pipeline
    assert "step: ACRDockerBuild" not in aliyun_pipeline
    assert "aliyun-kubectl" not in aliyun_pipeline
    assert "read_only: true" in ecs_compose
    assert "JUYA_PROCESS_ROLE: admin-beat" in ecs_compose


def test_local_compose_seeds_admin_after_migration_before_api() -> None:
    # 功能:验证本地 Compose 在迁移后、API 启动前初始化管理员。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证联系方式管理接口执行认证、CSRF、幂等和禁用缓存约束。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
            # 功能:返回联系方式修正申请列表的预设分页结果。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
            #     page: 分页页码,从第一页开始。
            #     page_size: 每页最多返回的记录数。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:ContactCorrectionPage,由本用例预设的数据或所组装的测试资源构成。
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
            # 功能:返回指定修正申请的预设详情。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     correction_id: 待查询或处理的联系方式修正申请标识。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:ContactCorrection,由本用例预设的数据或所组装的测试资源构成。
            del correction_id, admin_id
            return (await self.list_contact_corrections(None, 1, 20, "admin")).items[0]

        async def update_contact_status(
            self, user_id: str, status: str, admin_id: str
        ) -> ContactProjection:
            # 功能:模拟更新联系方式状态并记录调用参数。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
            #     status: 本测试预设的业务状态或返回状态,用于检查状态约束。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:ContactProjection,由本用例预设的数据或所组装的测试资源构成。
            return ContactProjection(user_id, "wx-private", status, False, None, admin_id, now)

        async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
            # 功能:模拟核实联系方式修改并返回预设投影。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:ContactProjection,由本用例预设的数据或所组装的测试资源构成。
            return ContactProjection(user_id, "wx-private", "PENDING", False, now, admin_id, now)

        async def decide_contact_correction(
            self,
            correction_id: str,
            decision: str,
            admin_id: str,
            idempotency_key: str,
        ) -> CorrectionDecision:
            # 功能:模拟审批联系方式修正申请并返回审批结果。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     correction_id: 待查询或处理的联系方式修正申请标识。
            #     decision: 联系方式修正审批结论,例如批准或拒绝。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            #     idempotency_key: 请求幂等键,用于匹配原请求并避免重复业务写入。
            # 返回:CorrectionDecision,由本用例预设的数据或所组装的测试资源构成。
            del admin_id, idempotency_key
            return CorrectionDecision(correction_id, decision, now)

    class AuditRepository:
        def __init__(self) -> None:
            # 功能:初始化 AuditRepository 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.events: list[AuditEvent] = []

        async def append(self, event: AuditEvent) -> None:
            # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            #     event: 待记录的审计或业务事件。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.events.append(event)

        async def list_recent(self, limit: int) -> list[AuditEvent]:
            # 功能:从测试仓库返回最近的指定数量审计事件。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
            # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
            return self.events[-limit:]

    async def current_admin(x_test_auth: str | None = Header(default=None)) -> SessionRecord:
        # 功能:提供当前测试的管理员认证依赖。
        # 参数:
        #     x_test_auth: 测试认证请求头,替代真实会话以检查认证依赖。
        # 返回:测试管理员会话。
        if x_test_auth != "ok":
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return session

    async def current_admin_write(
        x_test_auth: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        # 功能:提供当前测试的写操作认证及 CSRF 校验依赖。
        # 参数:
        #     x_test_auth: 测试认证请求头,替代真实会话以检查认证依赖。
        #     x_csrf_token: 写操作请求的 CSRF 令牌,与当前测试会话的预设值比较。
        # 返回:测试管理员会话。
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
        # 功能:为测试请求注入固定追踪标识并继续中间件链。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        #     call_next: ASGI 中间件下一个处理器,用于继续处理当前请求。
        # 返回:下游处理器返回的 HTTP 响应。
        request.state.request_id = "request-e2e"
        return await call_next(request)

    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
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


def test_user_routes_return_contact_and_learning_aggregates_without_cache() -> None:
    # 功能:验证用户接口返回联系方式与学习聚合信息且禁用缓存。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.modules.user_projection.router import create_operations_router
    from juya_admin_api.modules.user_projection.service import (
        InMemoryUserProjectionRepository,
        UserProjection,
        UserProjectionService,
    )

    now = datetime(2026, 9, 29, 3, 30, tzinfo=UTC)
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
        async def search_user_ids_by_wechat(
            self, wechat_id: str, admin_id: str = "system"
        ) -> tuple[str, ...]:
            # 功能:按微信号返回预设匹配用户标识并记录管理员上下文。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     wechat_id: 被搜索的微信号,模拟上游联系方式查询。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:tuple[str, ...],由本用例预设的数据或所组装的测试资源构成。
            del wechat_id, admin_id
            return ("user-1",)

        async def get_contact_projections(
            self, user_ids: tuple[str, ...], admin_id: str = "system"
        ) -> ContactProjectionResult:
            # 功能:按用户标识批量返回预设联系方式投影。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     user_ids: 需要批量获取联系方式投影的用户标识集合。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:ContactProjectionResult,由本用例预设的数据或所组装的测试资源构成。
            del admin_id
            return ContactProjectionResult(
                tuple(
                    ContactProjection(
                        user_id,
                        "wx-private",
                        "CONTACTED",
                        False,
                        now,
                        "admin-0",
                        now,
                    )
                    for user_id in user_ids
                ),
                False,
            )

        async def get_learning_overview(self, user_id: str, admin_id: str) -> LearningOverview:
            # 功能:返回测试用户的预设学习汇总信息。
            # 参数:
            #     self: 当前 Client 测试替身实例,保存本用例的预设状态或调用记录。
            #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
            #     admin_id: 执行操作的管理员标识,供权限上下文及审计归属检查。
            # 返回:LearningOverview,由本用例预设的数据或所组装的测试资源构成。
            del user_id, admin_id
            return LearningOverview(7, 12, 3)

    class AuditRepository:
        def __init__(self) -> None:
            # 功能:初始化 AuditRepository 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.events: list[AuditEvent] = []

        async def append(self, event: AuditEvent) -> None:
            # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            #     event: 待记录的审计或业务事件。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            self.events.append(event)

        async def list_recent(self, limit: int) -> list[AuditEvent]:
            # 功能:从测试仓库返回最近的指定数量审计事件。
            # 参数:
            #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
            #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
            # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
            return self.events[-limit:]

    async def current_admin(x_test_auth: str | None = Header(default=None)) -> SessionRecord:
        # 功能:提供当前测试的管理员认证依赖。
        # 参数:
        #     x_test_auth: 测试认证请求头,替代真实会话以检查认证依赖。
        # 返回:测试管理员会话。
        if x_test_auth != "ok":
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return session

    repository = InMemoryUserProjectionRepository()
    repository.users["user-1"] = UserProjection(
        "user-1", "ACTIVE", now, 1, 2, 0, contact_status="CONTACTED"
    )
    audit_repository = AuditRepository()
    service = UserProjectionService(repository, Client(), AuditService(audit_repository))
    app = FastAPI()
    install_error_handlers(app)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Any:
        # 功能:为测试请求注入固定追踪标识并继续中间件链。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        #     call_next: ASGI 中间件下一个处理器,用于继续处理当前请求。
        # 返回:下游处理器返回的 HTTP 响应。
        request.state.request_id = "request-users"
        return await call_next(request)

    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    app.include_router(
        create_operations_router(
            service,
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            current_admin=current_admin,
            clock=lambda: now,
        )
    )

    with TestClient(app) as client:
        listed = client.get(
            "/api/v1/admin/users",
            params={"contact_status": "CONTACTED"},
            headers={"X-Test-Auth": "ok"},
        )
        detail = client.get(
            "/api/v1/admin/users/user-1",
            headers={"X-Test-Auth": "ok"},
        )

    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()[0]["contact"]["wechat_id"] == "wx-private"
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert detail.json()["contact"]["change_pending"] is False
    assert detail.json()["open_scene_completed_count"] == 7
    assert detail.json()["learning_days"] == 12
    assert detail.json()["favorite_count"] == 3
    assert [event.action for event in audit_repository.events] == [
        "contact.view.list",
        "contact.view.detail",
    ]
