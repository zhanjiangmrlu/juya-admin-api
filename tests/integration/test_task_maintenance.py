import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.infrastructure.tasks.maintenance import (
    _aggregate_daily,
    _cleanup_feedback_screenshots,
    _dispatch_outbox,
    _refresh_time_sensitive_projections,
    _verify_daily_integrity,
)
from juya_admin_api.shared.ids import new_ulid

TEST_DATABASE_URL = os.getenv("JUYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JUYA_TEST_DATABASE_URL must point to an isolated MySQL 8.4 database",
)


@pytest.mark.asyncio
async def test_projection_analytics_and_integrity_jobs_execute_on_mysql() -> None:
    # 功能:验证投影、统计和完整性任务可在 MySQL 上执行。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert TEST_DATABASE_URL is not None
    settings = Settings(
        database_url=TEST_DATABASE_URL.replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    )

    refresh = await _refresh_time_sensitive_projections(settings)
    analytics = await _aggregate_daily(settings)
    integrity = await _verify_daily_integrity(settings)

    assert set(refresh) == {"expired_pending", "expired_active"}
    assert analytics["metric_count"] >= 30
    assert set(integrity) == {
        "expired_active_entitlements",
        "broken_audio_references",
        "healthy",
    }


@pytest.mark.asyncio
async def test_outbox_delivery_and_screenshot_cleanup_are_retry_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证发件箱投递及截图清理可安全重试。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert TEST_DATABASE_URL is not None
    now = datetime.now(UTC)
    ids = [new_ulid(now + timedelta(microseconds=offset)) for offset in range(6)]
    success_event, failed_event, success_ticket, failed_ticket, _, _ = ids
    sync_engine = create_engine(TEST_DATABASE_URL)
    with sync_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO admin_outbox "
                "(event_id, event_type, aggregate_public_id, payload, status, "
                "next_attempt_at, created_at) VALUES "
                "(:success, 'MINIAPP_MESSAGE', 'feedback-success', "
                "JSON_OBJECT('result', 'ok'), 'PENDING', :due, :now), "
                "(:failed, 'MINIAPP_MESSAGE', 'feedback-failed', "
                "JSON_OBJECT('result', 'fail'), 'PROCESSING', :due, :now)"
            ),
            {
                "success": success_event,
                "failed": failed_event,
                "due": now - timedelta(minutes=10),
                "now": now,
            },
        )
        for ticket_id, object_key in (
            (success_ticket, f"feedback/{success_ticket}/success.png"),
            (failed_ticket, f"feedback/{failed_ticket}/fail.png"),
        ):
            connection.execute(
                text(
                    "INSERT INTO feedback_ticket "
                    "(public_id, user_id, category, description, source, status, sla_hours, "
                    "create_idempotency_key, created_at, updated_at, resolved_at, closed_at) "
                    "VALUES (:public_id, NULL, 'FUNCTION', 'cleanup test', JSON_OBJECT(), "
                    "'RESOLVED', 48, :key, :now, :now, :now, :now)"
                ),
                {"public_id": ticket_id, "key": f"cleanup-{ticket_id}", "now": now},
            )
            ticket_pk = connection.scalar(text("SELECT LAST_INSERT_ID()"))
            connection.execute(
                text(
                    "INSERT INTO feedback_screenshot "
                    "(ticket_id, object_key, security_status, delete_after) "
                    "VALUES (:ticket_id, :object_key, 'PASSED', :due)"
                ),
                {
                    "ticket_id": ticket_pk,
                    "object_key": object_key,
                    "due": now - timedelta(days=1),
                },
            )

    class FakeMiniappClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            # 功能:初始化 FakeMiniappClient 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
            #     _args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。 当前替身
            #       保留该形参以兼容调用接口。
            #     _kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。 当前替身保留
            #       该形参以兼容调用接口。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pass

        async def create_message(self, payload: dict[str, object], _event_id: str) -> None:
            # 功能:记录小程序消息创建请求,供发件箱投递契约检查。
            # 参数:
            #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
            #     payload: 待提交的业务请求载荷;进程脚本中为标准输入文本。
            #     _event_id: 清理回调或业务事件的幂等标识。 当前替身保留该形参以兼容调用接口。
            # 返回:无;仅 payload 的 result 为 fail 时模拟消息投递失败。
            if payload["result"] == "fail":
                raise RuntimeError("temporary upstream failure")

        async def aclose(self) -> None:
            # 功能:模拟关闭异步客户端以符合运行时资源释放协议。
            # 参数:
            #     self: 当前 FakeMiniappClient 测试替身实例,保存本用例的预设状态或调用记录。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pass

    class FakeOssProvider:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            # 功能:初始化 FakeOssProvider 测试替身的预设数据和调用记录。
            # 参数:
            #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
            #     _args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。 当前替身
            #       保留该形参以兼容调用接口。
            #     _kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。 当前替身保留
            #       该形参以兼容调用接口。
            # 返回:无;完成模拟状态更新、调用记录或检查。
            pass

        async def delete_object(self, object_key: str) -> None:
            # 功能:模拟删除指定对象并记录清理操作。
            # 参数:
            #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
            #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
            # 返回:无;仅对象键以 fail.png 结尾时模拟删除失败。
            if object_key.endswith("fail.png"):
                raise RuntimeError("temporary OSS failure")

    monkeypatch.setattr(maintenance, "MiniappApiClient", FakeMiniappClient)
    monkeypatch.setattr(maintenance, "AliyunOssProvider", FakeOssProvider)
    settings = Settings(
        database_url=TEST_DATABASE_URL.replace("mysql+pymysql://", "mysql+asyncmy://", 1),
        internal_hmac_secret="integration-secret",
        oss_region="cn-hangzhou",
        oss_bucket="integration-private-bucket",
        oss_expected_bucket="integration-private-bucket",
        oss_access_key_id="test-id",
        oss_access_key_secret="test-secret",
    )

    try:
        outbox = await _dispatch_outbox(settings)
        cleanup = await _cleanup_feedback_screenshots(settings)

        assert outbox == {"delivered": 1, "failed": 1}
        assert cleanup == {"deleted": 1, "failed": 1}
        with sync_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT event_id, status, attempt_count FROM admin_outbox "
                    "WHERE event_id IN (:success, :failed)"
                ),
                {"success": success_event, "failed": failed_event},
            ).mappings()
            statuses = {row["event_id"]: (row["status"], row["attempt_count"]) for row in rows}
            assert statuses[success_event] == ("PUBLISHED", 1)
            assert statuses[failed_event] == ("PENDING", 1)
            deleted = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM feedback_screenshot s "
                    "JOIN feedback_ticket t ON t.id = s.ticket_id "
                    "WHERE t.public_id IN (:success, :failed) AND s.deleted_at IS NOT NULL"
                ),
                {"success": success_ticket, "failed": failed_ticket},
            )
            assert deleted == 1
    finally:
        with sync_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM admin_outbox WHERE event_id IN (:success, :failed)"),
                {"success": success_event, "failed": failed_event},
            )
            connection.execute(
                text("DELETE FROM feedback_ticket WHERE public_id IN (:success, :failed)"),
                {"success": success_ticket, "failed": failed_ticket},
            )
        sync_engine.dispose()
