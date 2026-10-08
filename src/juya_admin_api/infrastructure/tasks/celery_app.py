from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from kombu import Queue  # type: ignore[import-untyped]

from juya_admin_api.infrastructure.config import Settings


def create_celery_app(settings: Settings | None = None) -> Celery:
    # 功能: 创建 Celery 实例并加载任务队列和定时任务配置.
    # 参数:
    #     settings: 已加载并校验的服务运行配置.
    # 返回: 已配置任务调度的 Celery 应用.
    runtime_settings = settings or Settings()
    redis_url = (
        runtime_settings.redis_url.get_secret_value()
        if runtime_settings.redis_url is not None
        else "redis://localhost:6379/0"
    )
    app = Celery(
        "juya_admin_api",
        broker=redis_url,
        backend=redis_url,
        include=[
            "juya_admin_api.infrastructure.tasks.schedules",
            "juya_admin_api.modules.media.tasks",
        ],
    )
    app.conf.update(
        timezone="Asia/Shanghai",
        enable_utc=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        task_queues=tuple(
            Queue(name)
            for name in (
                "content.ocr",
                "content.audio",
                "content.assets",
                "content.publish",
                "content.lifecycle",
                "content.analytics",
                "domain.messages",
            )
        ),
        task_routes={
            "juya.content.ocr.*": {"queue": "content.ocr"},
            "juya.content.audio.*": {"queue": "content.audio"},
            "juya.content.assets.*": {"queue": "content.assets"},
            "juya.content.publish.*": {"queue": "content.publish"},
            "juya.content.lifecycle.*": {"queue": "content.lifecycle"},
            "juya.content.analytics.*": {"queue": "content.analytics"},
            "juya.domain.messages.*": {"queue": "domain.messages"},
        },
        beat_schedule={
            "recover-interrupted-content-batches": {
                "task": "juya.content.publish.batch_recover",
                "schedule": 60.0,
            },
            "refresh-time-sensitive-projections": {
                "task": "juya.content.lifecycle.refresh_time_sensitive_projections",
                "schedule": crontab(minute="*"),
            },
            "dispatch-domain-outbox": {
                "task": "juya.domain.messages.dispatch_outbox",
                "schedule": crontab(minute="*/5"),
            },
            "cleanup-feedback-screenshots": {
                "task": "juya.content.assets.cleanup_feedback_screenshots",
                "schedule": crontab(minute=0),
            },
            "cleanup-expired-drafts": {
                "task": "juya.content.assets.cleanup_expired_drafts",
                "schedule": crontab(minute=15),
            },
            "aggregate-daily-analytics": {
                "task": "juya.content.analytics.aggregate_daily",
                "schedule": crontab(hour=1, minute=0),
            },
            "verify-daily-integrity": {
                "task": "juya.content.lifecycle.verify_daily_integrity",
                "schedule": crontab(hour=2, minute=0),
            },
        },
    )
    return app


celery_app = create_celery_app()
