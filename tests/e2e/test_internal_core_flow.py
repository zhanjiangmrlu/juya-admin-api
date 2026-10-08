import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import schedules
from juya_admin_api.infrastructure.tasks.celery_app import create_celery_app
from juya_admin_api.infrastructure.tasks.schedules import (
    aggregate_daily,
    cleanup_feedback_screenshots,
    dispatch_outbox,
    refresh_time_sensitive_projections,
    verify_daily_integrity,
)


def test_celery_routes_and_schedules_cover_content_and_domain_workers() -> None:
    # 功能:验证 Celery 路由和调度覆盖内容与业务工作任务。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    app = create_celery_app(Settings(redis_url="redis://localhost:6379/15"))

    queues = {queue.name for queue in app.conf.task_queues}
    assert {
        "content.ocr",
        "content.audio",
        "content.assets",
        "content.publish",
        "content.lifecycle",
        "content.analytics",
        "domain.messages",
    } <= queues
    assert set(app.conf.beat_schedule) == {
        "recover-interrupted-content-batches",
        "refresh-time-sensitive-projections",
        "dispatch-domain-outbox",
        "cleanup-feedback-screenshots",
        "cleanup-expired-drafts",
        "aggregate-daily-analytics",
        "verify-daily-integrity",
    }
    assert app.conf.timezone == "Asia/Shanghai"
    assert app.conf.beat_schedule["recover-interrupted-content-batches"] == {
        "task": "juya.content.publish.batch_recover",
        "schedule": 60.0,
    }
    assert app.conf.beat_schedule["refresh-time-sensitive-projections"]["schedule"].minute == set(
        range(60)
    )
    assert app.conf.beat_schedule["dispatch-domain-outbox"]["schedule"].minute == set(
        range(0, 60, 5)
    )
    assert app.conf.beat_schedule["cleanup-feedback-screenshots"]["schedule"].minute == {0}
    assert app.conf.beat_schedule["aggregate-daily-analytics"]["schedule"].hour == {1}
    assert app.conf.beat_schedule["verify-daily-integrity"]["schedule"].hour == {2}


def test_periodic_task_entrypoints_are_serializable_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证周期任务入口可序列化且调度结果确定。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    # 匿名函数: 为内部任务提供更新时效性业务投影的可序列化替身响应。
    # 参数: 无。
    # 返回: operation 字段为 refresh_time_sensitive_projections 的字典。
    monkeypatch.setattr(
        schedules,
        "run_refresh_time_sensitive_projections",
        lambda: {"operation": "refresh_time_sensitive_projections"},
    )
    # 匿名函数: 为内部任务提供派发发件箱消息的可序列化替身响应。
    # 参数: 无。
    # 返回: operation 字段为 dispatch_outbox 的字典。
    monkeypatch.setattr(schedules, "run_dispatch_outbox", lambda: {"operation": "dispatch_outbox"})
    # 匿名函数: 为内部任务提供清理反馈截图的可序列化替身响应。
    # 参数: 无。
    # 返回: operation 字段为 cleanup_feedback_screenshots 的字典。
    monkeypatch.setattr(
        schedules,
        "run_cleanup_feedback_screenshots",
        lambda: {"operation": "cleanup_feedback_screenshots"},
    )
    # 匿名函数: 为内部任务提供汇总每日统计的可序列化替身响应。
    # 参数: 无。
    # 返回: operation 字段为 aggregate_daily 的字典。
    monkeypatch.setattr(schedules, "run_aggregate_daily", lambda: {"operation": "aggregate_daily"})
    # 匿名函数: 为内部任务提供检查每日业务完整性的可序列化替身响应。
    # 参数: 无。
    # 返回: operation 字段为 verify_daily_integrity 的字典。
    monkeypatch.setattr(
        schedules,
        "run_verify_daily_integrity",
        lambda: {"operation": "verify_daily_integrity"},
    )
    operations = {
        refresh_time_sensitive_projections()["operation"],
        dispatch_outbox()["operation"],
        cleanup_feedback_screenshots()["operation"],
        aggregate_daily()["operation"],
        verify_daily_integrity()["operation"],
    }

    assert operations == {
        "refresh_time_sensitive_projections",
        "dispatch_outbox",
        "cleanup_feedback_screenshots",
        "aggregate_daily",
        "verify_daily_integrity",
    }
