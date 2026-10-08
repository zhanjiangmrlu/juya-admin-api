import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command as alembic_command
from alembic.config import Config
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.db.session import create_engine as create_async_engine
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyEntitlementQueryRepository,
)
from juya_admin_api.modules.limited_entitlements.domain import LimitedEntitlement
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
)
from juya_admin_api.modules.limited_entitlements.router import create_limited_entitlement_router
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError, install_error_handlers

PROJECT_ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def command_client() -> AsyncIterator[tuple[AsyncClient, str, str]]:
    # 功能:提供含预设限时权益数据的 HTTP 客户端及其仓库。
    # 参数:无。
    # 返回:测试资源生成器;产生数据库连接或会话后,在退出时释放资源。
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")
    _seed(database_url)
    engine = create_async_engine(database_url.replace("mysql+pymysql", "mysql+asyncmy"))
    sessions = create_session_factory(engine)
    service = LimitedEntitlementService(SQLAlchemyLimitedEntitlementRepository(sessions))
    entitlement = await service.grant(
        "01J00000000000000000000400", "01J00000000000000000000410", "7", "initial-grant", NOW
    )
    await service.activate_for_scene(entitlement.user_id, "01J00000000000000000000420", NOW)

    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        # 功能:检查测试认证头并返回预设管理员会话。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        # 返回:测试管理员会话。
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效", 401)
        return SessionRecord("session", 7, "token", "csrf", "test", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        # 功能:在测试认证通过后检查写请求 CSRF 令牌。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        #     x_csrf_token: 写操作请求的 CSRF 令牌,与当前测试会话的预设值比较。
        # 返回:测试管理员会话。
        admin = await read_admin(x_test_admin)
        if x_csrf_token != "csrf":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF token 无效", 403)
        return admin

    ticks = 0

    def clock() -> datetime:
        # 功能:提供可控测试时间,避免真实时钟影响重试与期限断言。
        # 参数:无。
        # 返回:当前预设或按调用次数推进的测试时间。
        nonlocal ticks
        ticks += 1
        return NOW + timedelta(seconds=ticks)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_limited_entitlement_router(
            service,
            query_repository=SQLAlchemyEntitlementQueryRepository(sessions),
            current_admin=read_admin,
            current_admin_write=write_admin,
            clock=clock,
        )
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, entitlement.id, database_url
    finally:
        await engine.dispose()


COMMAND_HEADERS = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "resume", "revoke"])
async def test_limited_commands_require_auth_csrf_and_idempotency_key(
    command_client: tuple[AsyncClient, str, str], operation: str
) -> None:
    # 功能:验证限时权益命令要求认证、CSRF 和幂等键。
    # 参数:
    #     command_client: 已组装限时权益路由的测试客户端及其内存仓库。
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, entitlement_id, _ = command_client
    path = f"/api/v1/admin/limited-entitlements/{entitlement_id}/commands/{operation}"
    body = {} if operation == "resume" else {"reason": "核对"}
    assert (await client.post(path, json=body)).status_code == 401
    assert (await client.post(path, json=body, headers={"X-Test-Admin": "1"})).status_code == 403
    assert (await client.post(path, json=body, headers=COMMAND_HEADERS)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "resume", "revoke"])
async def test_limited_command_immediate_retry_replays_stored_result_once(
    command_client: tuple[AsyncClient, str, str], operation: str
) -> None:
    # 功能:验证限时权益立即重试只重放一次已保存结果。
    # 参数:
    #     command_client: 已组装限时权益路由的测试客户端及其内存仓库。
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, entitlement_id, database_url = command_client
    base = f"/api/v1/admin/limited-entitlements/{entitlement_id}/commands"
    if operation in {"resume", "revoke"}:
        setup = await client.post(
            f"{base}/pause",
            json={"reason": "先暂停"},
            headers={**COMMAND_HEADERS, "X-Idempotency-Key": "setup-pause"},
        )
        assert setup.status_code == 200
    body = {} if operation == "resume" else {"reason": "核对"}
    headers = {**COMMAND_HEADERS, "X-Idempotency-Key": "command-once"}
    first = await client.post(f"{base}/{operation}", json=body, headers=headers)
    retry = await client.post(f"{base}/{operation}", json=body, headers=headers)
    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json() == first.json()
    verify_engine = create_engine(database_url)
    with verify_engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM limited_entitlement_operation "
                    "WHERE idempotency_key = 'command-once'"
                )
            )
            == 1
        )
    verify_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,opposite", [("pause", "resume"), ("resume", "pause")])
async def test_limited_delayed_retry_after_opposite_command_does_not_execute_again(
    command_client: tuple[AsyncClient, str, str], operation: str, opposite: str
) -> None:
    # 功能:验证相反操作后的限时权益延迟重试不再次执行。
    # 参数:
    #     command_client: 已组装限时权益路由的测试客户端及其内存仓库。
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    #     opposite: 首次命令之后执行的相反操作,用于验证延迟重放。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, entitlement_id, database_url = command_client
    base = f"/api/v1/admin/limited-entitlements/{entitlement_id}/commands"
    if operation == "resume":
        await client.post(
            f"{base}/pause",
            json={"reason": "先暂停"},
            headers={**COMMAND_HEADERS, "X-Idempotency-Key": "setup-pause"},
        )
    headers = {**COMMAND_HEADERS, "X-Idempotency-Key": "original-command"}
    body = {} if operation == "resume" else {"reason": "核对"}
    first = await client.post(f"{base}/{operation}", json=body, headers=headers)
    assert first.status_code == 200
    changed = await client.post(
        f"{base}/{opposite}",
        json={"reason": "再次暂停"} if opposite == "pause" else {},
        headers={**COMMAND_HEADERS, "X-Idempotency-Key": "opposite-command"},
    )
    assert changed.status_code == 200
    retry = await client.post(f"{base}/{operation}", json=body, headers=headers)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    verify_engine = create_engine(database_url)
    with verify_engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, version FROM limited_entitlement WHERE public_id = :id"),
            {"id": entitlement_id},
        ).one()
        assert row.status == changed.json()["status"]
        assert row.version == changed.json()["version"]
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM limited_entitlement_operation "
                    "WHERE idempotency_key = 'original-command'"
                )
            )
            == 1
        )
    verify_engine.dispose()


@pytest.mark.asyncio
async def test_limited_command_same_key_different_request_conflicts(
    command_client: tuple[AsyncClient, str, str],
) -> None:
    # 功能:验证限时权益同一键承载不同请求会冲突。
    # 参数:
    #     command_client: 已组装限时权益路由的测试客户端及其内存仓库。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, entitlement_id, _ = command_client
    base = f"/api/v1/admin/limited-entitlements/{entitlement_id}/commands"
    headers = {**COMMAND_HEADERS, "X-Idempotency-Key": "bound-request"}
    assert (
        await client.post(f"{base}/pause", json={"reason": "核对"}, headers=headers)
    ).status_code == 200
    for operation, body in [
        ("pause", {"reason": "不同原因"}),
        ("revoke", {"reason": "核对"}),
        ("resume", {}),
    ]:
        response = await client.post(f"{base}/{operation}", json=body, headers=headers)
        assert response.status_code == 409
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_capacity_one_and_fifty_concurrent_opens_are_atomic() -> None:
    # 功能:验证容量为一时五十个并发开通请求保持原子性。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    alembic_command.upgrade(config, "head")
    _seed(database_url)

    async_url = database_url.replace("mysql+pymysql", "mysql+asyncmy")
    engine = create_async_engine(async_url)
    service = LimitedEntitlementService(
        SQLAlchemyLimitedEntitlementRepository(create_session_factory(engine))
    )
    grants = await asyncio.gather(
        service.grant(
            "01J00000000000000000000400",
            "01J00000000000000000000410",
            "admin-1",
            "grant-user-1",
            NOW,
        ),
        service.grant(
            "01J00000000000000000000401",
            "01J00000000000000000000410",
            "admin-1",
            "grant-user-2",
            NOW,
        ),
        return_exceptions=True,
    )
    successes = [item for item in grants if isinstance(item, LimitedEntitlement)]
    failures = [item for item in grants if isinstance(item, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "CAMPAIGN_CAPACITY_REACHED"

    user_id = successes[0].user_id
    decisions = await asyncio.gather(
        *(service.activate_for_scene(user_id, "01J00000000000000000000420", NOW) for _ in range(50))
    )
    await engine.dispose()

    assert all(isinstance(item, AccessDecision) for item in decisions)
    assert all(item.level is AccessLevel.LIMITED for item in decisions)
    assert {item.activated_at for item in decisions} == {NOW}
    assert len({item.earliest_expires_at for item in decisions}) == 1

    verify_engine = create_engine(database_url)
    with verify_engine.connect() as connection:
        granted_count = connection.scalar(
            text(
                "SELECT granted_user_count FROM limited_campaign_version "
                "WHERE public_id = '01J00000000000000000000410'"
            )
        )
        activation_count = connection.scalar(
            text(
                "SELECT COUNT(DISTINCT activated_at) FROM limited_entitlement "
                "WHERE campaign_version_id = (SELECT id FROM limited_campaign_version "
                "WHERE public_id = '01J00000000000000000000410')"
            )
        )
    verify_engine.dispose()
    assert granted_count == 1
    assert activation_count == 1


def _seed(database_url: str) -> None:
    # 功能:在独立测试数据库插入当前用例需要的业务事实。
    # 参数:
    #     database_url: 独立测试数据库的连接 URL,由测试环境提供。
    # 返回:无;完成模拟状态更新、调用记录或检查。
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in (
            "limited_entitlement_operation",
            "limited_entitlement",
            "limited_campaign_scene",
            "limited_campaign_version",
            "limited_campaign",
            "scene_revision",
            "scene",
            "content_template",
            "content_series",
            "user_account",
        ):
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        connection.execute(
            text(
                "INSERT INTO user_account (public_id, juya_number, status) VALUES "
                "('01J00000000000000000000400', 'JUYA-400', 'ACTIVE'), "
                "('01J00000000000000000000401', 'JUYA-401', 'ACTIVE')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO content_series (public_id, slug, title, status) "
                "VALUES ('01J00000000000000000000402', 'limited', 'Limited', 'PUBLISHED')"
            )
        )
        series_id = connection.scalar(text("SELECT id FROM content_series"))
        connection.execute(
            text(
                "INSERT INTO content_template "
                "(template_type, version, required_modules, validation_rules) "
                "VALUES ('limited', 1, JSON_ARRAY(), JSON_OBJECT())"
            )
        )
        template_id = connection.scalar(text("SELECT id FROM content_template"))
        connection.execute(
            text(
                "INSERT INTO scene (public_id, series_id, template_id, title, status) "
                "VALUES ('01J00000000000000000000420', :series_id, :template_id, "
                "'Limited Scene', 'PUBLISHED')"
            ),
            {"series_id": series_id, "template_id": template_id},
        )
        scene_id = connection.scalar(text("SELECT id FROM scene"))
        connection.execute(
            text(
                "INSERT INTO scene_revision "
                "(public_id, scene_id, version_no, status, content_snapshot, created_by, "
                "published_at) VALUES ('01J00000000000000000000421', :scene_id, 1, "
                "'PUBLISHED', JSON_OBJECT(), 'admin-1', :now)"
            ),
            {"scene_id": scene_id, "now": NOW},
        )
        revision_id = connection.scalar(text("SELECT id FROM scene_revision"))
        connection.execute(
            text("UPDATE scene SET published_revision_id = :revision_id WHERE id = :scene_id"),
            {"revision_id": revision_id, "scene_id": scene_id},
        )
        connection.execute(
            text(
                "INSERT INTO limited_campaign (public_id, name, status) "
                "VALUES ('01J00000000000000000000409', 'Trial', 'OPEN')"
            )
        )
        campaign_id = connection.scalar(text("SELECT id FROM limited_campaign"))
        connection.execute(
            text(
                "INSERT INTO limited_campaign_version "
                "(public_id, campaign_id, version_no, status, duration_days, "
                "activation_window_days, capacity) VALUES "
                "('01J00000000000000000000410', :campaign_id, 1, 'OPEN', 3, 7, 1)"
            ),
            {"campaign_id": campaign_id},
        )
        version_id = connection.scalar(text("SELECT id FROM limited_campaign_version"))
        connection.execute(
            text(
                "UPDATE limited_campaign SET current_version_id = :version_id "
                "WHERE id = :campaign_id"
            ),
            {"version_id": version_id, "campaign_id": campaign_id},
        )
        connection.execute(
            text(
                "INSERT INTO limited_campaign_scene "
                "(campaign_version_id, scene_id, position, scene_revision_id) "
                "VALUES (:version_id, :scene_id, 1, :revision_id)"
            ),
            {
                "version_id": version_id,
                "scene_id": scene_id,
                "revision_id": revision_id,
            },
        )
    engine.dispose()
