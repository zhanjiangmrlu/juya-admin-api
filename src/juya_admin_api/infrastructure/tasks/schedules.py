from typing import Any

from juya_admin_api.infrastructure.tasks.celery_app import celery_app
from juya_admin_api.infrastructure.tasks.maintenance import (
    run_aggregate_daily,
    run_cleanup_expired_drafts,
    run_cleanup_feedback_screenshots,
    run_dispatch_outbox,
    run_refresh_time_sensitive_projections,
    run_verify_daily_integrity,
)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.lifecycle.refresh_time_sensitive_projections"
)
def refresh_time_sensitive_projections() -> dict[str, Any]:
    # 功能: 执行定时权益及时间敏感状态刷新任务.
    # 参数: 无.
    # 返回: 本次未开始失效及已激活到期的权益数量.
    return run_refresh_time_sensitive_projections()


@celery_app.task(name="juya.domain.messages.dispatch_outbox")  # type: ignore[untyped-decorator]
def dispatch_outbox() -> dict[str, Any]:
    # 功能: 执行定时 outbox 消息派发任务.
    # 参数: 无.
    # 返回: 本次通知送达数和失败数.
    return run_dispatch_outbox()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.assets.cleanup_feedback_screenshots"
)
def cleanup_feedback_screenshots() -> dict[str, Any]:
    # 功能: 执行定时反馈截图清理任务.
    # 参数: 无.
    # 返回: 本次成功删除和删除失败的截图数量.
    return run_cleanup_feedback_screenshots()


@celery_app.task(name="juya.content.assets.cleanup_expired_drafts")  # type: ignore[untyped-decorator]
def cleanup_expired_drafts() -> dict[str, Any]:
    # 功能: 执行定时过期制作草稿清理任务.
    # 参数: 无.
    # 返回: 本次清理草稿数及受任务保护的草稿数.
    return run_cleanup_expired_drafts()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.analytics.aggregate_daily"
)
def aggregate_daily() -> dict[str, Any]:
    # 功能: 执行定时每日统计聚合任务.
    # 参数: 无.
    # 返回: 汇总日期,快照日期,指标数,事件数及重算的批次日期.
    return run_aggregate_daily()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="juya.content.lifecycle.verify_daily_integrity"
)
def verify_daily_integrity() -> dict[str, Any]:
    # 功能: 执行定时业务数据完整性检查任务.
    # 参数: 无.
    # 返回: 过期仍激活权益数,损坏音频引用数及是否健康.
    return run_verify_daily_integrity()
