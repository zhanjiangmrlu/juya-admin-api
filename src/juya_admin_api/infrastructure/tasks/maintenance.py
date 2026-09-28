import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider


def run_refresh_time_sensitive_projections() -> dict[str, Any]:
    return asyncio.run(_refresh_time_sensitive_projections(Settings()))


async def _refresh_time_sensitive_projections(settings: Settings) -> dict[str, Any]:
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            expired_pending = await session.execute(
                text(
                    "UPDATE limited_entitlement SET status = 'START_EXPIRED', "
                    "version = version + 1, updated_at = UTC_TIMESTAMP(6) "
                    "WHERE status = 'PENDING' AND start_deadline <= UTC_TIMESTAMP(6)"
                )
            )
            expired_active = await session.execute(
                text(
                    "UPDATE limited_entitlement SET status = 'ENDED', version = version + 1, "
                    "updated_at = UTC_TIMESTAMP(6) WHERE status = 'ACTIVE' "
                    "AND expires_at <= UTC_TIMESTAMP(6)"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO user_admin_projection "
                    "(user_id, account_status, last_active_at, formal_entitlement_count, "
                    "limited_entitlement_count, open_feedback_count, projection_version, "
                    "updated_at) SELECT u.id, u.status, MAX(lp.last_learned_at), "
                    "COUNT(DISTINCT fe.id), COUNT(DISTINCT le.id), COUNT(DISTINCT ft.id), "
                    "1, UTC_TIMESTAMP(6) FROM user_account u "
                    "LEFT JOIN learning_progress lp ON lp.user_id = u.id "
                    "LEFT JOIN formal_entitlement fe ON fe.user_id = u.id "
                    "AND fe.status IN ('ACTIVE','PAUSED') "
                    "LEFT JOIN limited_entitlement le ON le.user_id = u.id "
                    "AND le.status IN ('PENDING','ACTIVE','PAUSED') "
                    "LEFT JOIN feedback_ticket ft ON ft.user_id = u.id "
                    "AND ft.status IN ('PENDING','PROCESSING','NEED_MORE','USER_SUPPLIED') "
                    "GROUP BY u.id, u.status ON DUPLICATE KEY UPDATE "
                    "account_status = VALUES(account_status), "
                    "last_active_at = VALUES(last_active_at), "
                    "formal_entitlement_count = VALUES(formal_entitlement_count), "
                    "limited_entitlement_count = VALUES(limited_entitlement_count), "
                    "open_feedback_count = VALUES(open_feedback_count), "
                    "projection_version = projection_version + 1, "
                    "updated_at = VALUES(updated_at)"
                )
            )
        return {
            "expired_pending": int(getattr(expired_pending, "rowcount", 0)),
            "expired_active": int(getattr(expired_active, "rowcount", 0)),
        }
    finally:
        await engine.dispose()


def run_dispatch_outbox() -> dict[str, Any]:
    return asyncio.run(_dispatch_outbox(Settings()))


async def _dispatch_outbox(settings: Settings) -> dict[str, Any]:
    if settings.internal_hmac_secret is None:
        raise RuntimeError("JUYA_INTERNAL_HMAC_SECRET is required")
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    client = MiniappApiClient(
        settings.miniapp_api_base_url,
        settings.internal_hmac_secret.get_secret_value().encode(),
    )
    delivered = 0
    failed = 0
    try:
        async with factory() as session, session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT id, event_id, payload FROM admin_outbox "
                        "WHERE event_type = 'MINIAPP_MESSAGE' AND ((status = 'PENDING' "
                        "AND (next_attempt_at IS NULL OR next_attempt_at <= UTC_TIMESTAMP(6))) "
                        "OR (status = 'PROCESSING' AND next_attempt_at <= UTC_TIMESTAMP(6))) "
                        "ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED"
                    )
                )
            ).all()
            ids = [row.id for row in rows]
            if ids:
                placeholders = ",".join(str(int(value)) for value in ids)
                await session.execute(
                    text(
                        f"UPDATE admin_outbox SET status = 'PROCESSING', "
                        f"next_attempt_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 5 MINUTE) "
                        f"WHERE id IN ({placeholders})"
                    )
                )
        for row in rows:
            payload = _json_dict(row.payload)
            try:
                await client.create_message(payload, row.event_id)
            except Exception:
                failed += 1
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE admin_outbox SET status = 'PENDING', "
                            "attempt_count = attempt_count + 1, "
                            "next_attempt_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 60 SECOND) "
                            "WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
            else:
                delivered += 1
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE admin_outbox SET status = 'PUBLISHED', "
                            "attempt_count = attempt_count + 1, next_attempt_at = NULL, "
                            "published_at = UTC_TIMESTAMP(6) "
                            "WHERE id = :id"
                        ),
                        {"id": row.id},
                    )
        return {"delivered": delivered, "failed": failed}
    finally:
        await client.aclose()
        await engine.dispose()


def run_cleanup_feedback_screenshots() -> dict[str, Any]:
    return asyncio.run(_cleanup_feedback_screenshots(Settings()))


async def _cleanup_feedback_screenshots(settings: Settings) -> dict[str, Any]:
    if not settings.oss_region or not settings.oss_bucket:
        raise RuntimeError("JUYA_OSS_REGION and JUYA_OSS_BUCKET are required")
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    oss = AliyunOssProvider(
        settings.oss_region, settings.oss_bucket, endpoint=settings.oss_endpoint
    )
    deleted = 0
    failed = 0
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, object_key FROM feedback_screenshot "
                        "WHERE deleted_at IS NULL AND delete_after <= UTC_TIMESTAMP(6) "
                        "ORDER BY id LIMIT 100"
                    )
                )
            ).all()
        for row in rows:
            try:
                await oss.delete_object(row.object_key)
            except Exception:
                failed += 1
                continue
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE feedback_screenshot SET deleted_at = UTC_TIMESTAMP(6) "
                        "WHERE id = :id AND deleted_at IS NULL"
                    ),
                    {"id": row.id},
                )
            deleted += 1
        return {"deleted": deleted, "failed": failed}
    finally:
        await engine.dispose()


def run_aggregate_daily() -> dict[str, Any]:
    return asyncio.run(_aggregate_daily(Settings()))


async def _aggregate_daily(settings: Settings) -> dict[str, Any]:
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    day = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    metrics = {
        "NEW_USERS": (
            "SELECT COUNT(*) FROM user_account "
            "WHERE DATE(CONVERT_TZ(created_at, '+00:00', '+08:00')) = :day"
        ),
        "ACTIVE_USERS": (
            "SELECT COUNT(*) FROM user_admin_projection WHERE account_status = 'ACTIVE'"
        ),
        "FEEDBACK_SLA": (
            "SELECT COUNT(*) FROM feedback_ticket "
            "WHERE DATE(CONVERT_TZ(created_at, '+00:00', '+08:00')) = :day "
            "AND deadline_at >= COALESCE(resolved_at, closed_at, UTC_TIMESTAMP(6))"
        ),
        "DELETIONS": (
            "SELECT COUNT(*) FROM deletion_cleanup_event "
            "WHERE DATE(CONVERT_TZ(completed_at, '+00:00', '+08:00')) = :day"
        ),
    }
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM analytics_daily WHERE metric_day = :day"), {"day": day}
            )
            for metric, query in metrics.items():
                value = int(await session.scalar(text(query), {"day": day}) or 0)
                await session.execute(
                    text(
                        "INSERT INTO analytics_daily "
                        "(metric_day, metric, dimension, metric_value, generated_at) "
                        "VALUES (:day, :metric, 'ALL', :value, UTC_TIMESTAMP(6))"
                    ),
                    {"day": day, "metric": metric, "value": value},
                )
        return {"day": day.isoformat(), "metric_count": len(metrics)}
    finally:
        await engine.dispose()


def run_verify_daily_integrity() -> dict[str, Any]:
    return asyncio.run(_verify_daily_integrity(Settings()))


async def _verify_daily_integrity(settings: Settings) -> dict[str, Any]:
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            expired_access = int(
                await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM limited_entitlement WHERE status = 'ACTIVE' "
                        "AND expires_at <= UTC_TIMESTAMP(6)"
                    )
                )
                or 0
            )
            broken_audio = int(
                await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM audio_target t LEFT JOIN audio_version v "
                        "ON v.id = t.active_version_id WHERE t.active_version_id IS NOT NULL "
                        "AND v.id IS NULL"
                    )
                )
                or 0
            )
        return {
            "expired_active_entitlements": expired_access,
            "broken_audio_references": broken_audio,
            "healthy": expired_access == 0 and broken_audio == 0,
        }
    finally:
        await engine.dispose()


def _database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise RuntimeError("JUYA_DATABASE_URL is required")
    return settings.database_url.get_secret_value().replace(
        "mysql+pymysql://", "mysql+asyncmy://", 1
    )


def _json_dict(value: object) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return dict(decoded) if isinstance(decoded, dict) else {}
