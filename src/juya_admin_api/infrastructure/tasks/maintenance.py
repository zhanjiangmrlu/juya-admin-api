import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider
from juya_admin_api.integrations.oss.provider import validate_object_key
from juya_admin_api.modules.analytics.events import (
    EVENT_METRICS,
    AnalyticsEvent,
    aggregate_events,
    append_event,
)
from juya_admin_api.modules.analytics.service import EXPORTABLE_METRICS
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def run_refresh_time_sensitive_projections() -> dict[str, Any]:
    return asyncio.run(_refresh_time_sensitive_projections(Settings()))


async def _refresh_time_sensitive_projections(settings: Settings) -> dict[str, Any]:
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            now = datetime.now(UTC)
            expired_rows = (
                await session.execute(
                    text(
                        "SELECT le.public_id,le.user_id,le.status,le.granted_at,le.activated_at, "
                        "le.start_deadline,le.expires_at,cv.duration_days,"
                        "c.public_id AS campaign_id "
                        "FROM limited_entitlement le JOIN limited_campaign_version cv "
                        "ON cv.id=le.campaign_version_id "
                        "JOIN limited_campaign c ON c.id=cv.campaign_id "
                        "WHERE (le.status='PENDING' AND le.start_deadline<=UTC_TIMESTAMP(6)) "
                        "OR (le.status='ACTIVE' AND le.expires_at<=UTC_TIMESTAMP(6)) FOR UPDATE"
                    )
                )
            ).all()
            for row in expired_rows:
                pending = row.status == "PENDING"
                at = row.start_deadline if pending else row.expires_at
                at = at.replace(tzinfo=UTC) if at.tzinfo is None else at
                await append_event(
                    session,
                    event_key=f"limited-expiry:{row.public_id}:{row.status}",
                    event_type="LIMITED_START_EXPIRED" if pending else "LIMITED_EXPIRED",
                    user_id=row.user_id,
                    occurred_at=at,
                    dimension=f"campaign:{row.campaign_id}",
                    payload={
                        "mode": row.duration_days,
                        "cohort_day": row.granted_at.replace(tzinfo=UTC)
                        .astimezone(ZoneInfo("Asia/Shanghai"))
                        .date()
                        .isoformat(),
                    },
                )
            overdue = (
                await session.execute(
                    text(
                        "SELECT id,public_id,user_id,category,created_at,deadline_at "
                        "FROM feedback_ticket "
                        "WHERE status IN ('PENDING','PROCESSING','USER_SUPPLIED') "
                        "AND deadline_at<=UTC_TIMESTAMP(6) FOR UPDATE"
                    )
                )
            ).all()
            for row in overdue:
                await append_event(
                    session,
                    event_key=f"feedback-overdue:{row.public_id}",
                    event_type="FEEDBACK_OVERDUE",
                    user_id=row.user_id,
                    occurred_at=now,
                    payload={
                        "category": row.category,
                        "created_day": row.created_at.replace(tzinfo=UTC)
                        .astimezone(ZoneInfo("Asia/Shanghai"))
                        .date()
                        .isoformat(),
                    },
                )
            formal_expired = (
                await session.execute(
                    text(
                        "SELECT fe.public_id,fe.user_id,fe.expires_at,p.public_id AS package_id "
                        "FROM formal_entitlement fe JOIN content_package p ON p.id=fe.package_id "
                        "WHERE fe.status IN ('ACTIVE','PAUSED') "
                        "AND fe.expires_at<=UTC_TIMESTAMP(6) "
                        "AND NOT EXISTS (SELECT 1 FROM analytics_event e "
                        "WHERE BINARY e.event_key="
                        "BINARY CONCAT('formal-expired:',fe.public_id)) FOR UPDATE"
                    )
                )
            ).all()
            for row in formal_expired:
                await append_event(
                    session,
                    event_key=f"formal-expired:{row.public_id}",
                    event_type="FORMAL_EXPIRED",
                    user_id=row.user_id,
                    occurred_at=row.expires_at.replace(tzinfo=UTC),
                    dimension=f"package:{row.package_id}",
                )
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
        lease_until = datetime.now(UTC) + timedelta(minutes=5)
        async with factory() as session, session.begin():
            rows = (
                await session.execute(
                    text(
                        "SELECT id, event_id, event_type, payload, attempt_count FROM admin_outbox "
                        "WHERE event_type IN ('MINIAPP_MESSAGE','DELETION_CLEANUP_RESULT') "
                        "AND ((status = 'PENDING' "
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
                        f"next_attempt_at = :lease "
                        f"WHERE id IN ({placeholders})"
                    ),
                    {"lease": lease_until},
                )
        for row in rows:
            payload = _json_dict(row.payload)
            try:
                if row.event_type == "DELETION_CLEANUP_RESULT":
                    user_id = payload.get("user_id")
                    request_id = payload.get("deletion_request_id")
                    if (
                        not isinstance(user_id, str)
                        or not isinstance(request_id, str)
                        or payload.get("succeeded") is not True
                    ):
                        raise ValueError("Invalid deletion callback payload")
                    await client.record_deletion_cleanup_result(user_id, request_id, row.event_id)
                else:
                    await client.create_message(payload, row.event_id)
            except Exception:
                failed += 1
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE admin_outbox SET status = 'PENDING', "
                            "attempt_count = attempt_count + 1, "
                            "next_attempt_at = :retry "
                            "WHERE id = :id AND status='PROCESSING' AND next_attempt_at=:lease"
                        ),
                        {
                            "id": row.id,
                            "lease": lease_until,
                            "retry": datetime.now(UTC)
                            + timedelta(
                                seconds=min(3600, 60 * 2 ** min(int(row.attempt_count), 6))
                            ),
                        },
                    )
            else:
                delivered += 1
                async with factory() as session, session.begin():
                    result = await session.execute(
                        text(
                            "UPDATE admin_outbox SET status = 'PUBLISHED', "
                            "attempt_count = attempt_count + 1, next_attempt_at = NULL, "
                            "published_at = UTC_TIMESTAMP(6) "
                            "WHERE id = :id AND status='PROCESSING' AND next_attempt_at=:lease"
                        ),
                        {"id": row.id, "lease": lease_until},
                    )
                    if row.event_type == "DELETION_CLEANUP_RESULT" and getattr(
                        result, "rowcount", 0
                    ):
                        # Release the one-day protected retry delay once the consumer confirms.
                        await session.execute(
                            text(
                                "UPDATE feedback_screenshot SET delete_after=UTC_TIMESTAMP(6) "
                                "WHERE deleted_at IS NULL AND JSON_CONTAINS(:ids,CAST(id AS JSON))"
                            ),
                            {"ids": json.dumps(payload.get("screenshot_ids", []))},
                        )
        return {"delivered": delivered, "failed": failed}
    finally:
        await client.aclose()
        await engine.dispose()


def run_cleanup_feedback_screenshots() -> dict[str, Any]:
    return asyncio.run(_cleanup_feedback_screenshots(Settings()))


async def _cleanup_feedback_screenshots(settings: Settings) -> dict[str, Any]:
    settings.validate_oss_configuration()
    assert settings.oss_region is not None and settings.oss_bucket is not None
    engine = create_engine(_database_url(settings)).execution_options(
        isolation_level="REPEATABLE READ"
    )
    factory = create_session_factory(engine)
    oss = AliyunOssProvider(
        settings.oss_region,
        settings.oss_bucket,
        endpoint=settings.oss_endpoint,
        credentials_provider=ControlledCredentialsProvider(
            mode=settings.oss_credentials_mode,
            role_name=settings.oss_ram_role_name,
            access_key_id=settings.oss_access_key_id.get_secret_value()
            if settings.oss_access_key_id
            else None,
            access_key_secret=settings.oss_access_key_secret.get_secret_value()
            if settings.oss_access_key_secret
            else None,
            security_token=settings.oss_session_token.get_secret_value()
            if settings.oss_session_token
            else None,
            expires_at=settings.oss_credentials_expires_at,
            from_environment=True,
        ),
    )
    deleted = 0
    failed = 0
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id FROM feedback_screenshot "
                        "WHERE deleted_at IS NULL AND delete_after <= UTC_TIMESTAMP(6) "
                        "ORDER BY delete_after,id LIMIT 100"
                    )
                )
            ).all()
        for row in rows:
            async with factory() as session, session.begin():
                # Lock both the screenshot and ticket, then recheck the due state.
                current = (
                    await session.execute(
                        text(
                            "SELECT s.id,s.object_key,t.public_id,t.status "
                            "FROM feedback_screenshot s "
                            "JOIN feedback_ticket t ON t.id=s.ticket_id WHERE s.id=:id "
                            "AND s.deleted_at IS NULL AND s.delete_after<=UTC_TIMESTAMP(6) "
                            "FOR UPDATE SKIP LOCKED"
                        ),
                        {"id": row.id},
                    )
                ).first()
                if current is None:
                    continue
                try:
                    validate_object_key(current.object_key)
                    valid_key = current.object_key.startswith(
                        "feedback/"
                    ) and not current.object_key.endswith("/")
                except AppError:
                    valid_key = False
                deletion_captured = await _screenshot_deletion_captured(session, current.id)
                deletion_completed = (
                    deletion_captured
                    and await _screenshot_user_deletion_completed(session, current.id)
                )
                protected = (
                    not valid_key
                    or (deletion_captured and not deletion_completed)
                    or (
                        current.status not in {"RESOLVED", "CLOSED_INSUFFICIENT"}
                        and not deletion_completed
                    )
                    or await _screenshot_has_references(session, current.id, current.object_key)
                )
                if protected:
                    await _audit_screenshot_cleanup(session, current, "PROTECTED")
                    await _defer_screenshot_cleanup(session, current.id)
                    continue
                # Audit intent precedes the external delete. If audit SQL fails, do not delete.
                audit_id = await _audit_screenshot_cleanup(session, current, "DELETE_PENDING")
                try:
                    await oss.delete_object(current.object_key)
                except Exception:
                    failed += 1
                    outcome = "FAILED"
                    await _defer_screenshot_cleanup(session, current.id)
                else:
                    await session.execute(
                        text(
                            "UPDATE feedback_screenshot SET deleted_at=UTC_TIMESTAMP(6) "
                            "WHERE id=:id"
                        ),
                        {"id": current.id},
                    )
                    outcome = "DELETED"
                    deleted += 1
                await session.execute(
                    text(
                        "UPDATE audit_event SET after_summary="
                        "JSON_SET(after_summary,'$.outcome',:outcome) "
                        "WHERE public_id=:id"
                    ),
                    {"id": audit_id, "outcome": outcome},
                )
        return {"deleted": deleted, "failed": failed}
    finally:
        await engine.dispose()


async def _defer_screenshot_cleanup(session: AsyncSession, screenshot_id: int) -> None:
    # Protected/failed rows remain intact but cannot monopolize the next bounded batch.
    await session.execute(
        text(
            "UPDATE feedback_screenshot SET delete_after="
            "DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 1 DAY) WHERE id=:id"
        ),
        {"id": screenshot_id},
    )


async def _screenshot_deletion_captured(session: AsyncSession, screenshot_id: int) -> bool:
    return bool(
        await session.scalar(
            text(
                "SELECT 1 FROM admin_outbox WHERE event_type='DELETION_CLEANUP_RESULT' "
                "AND "
                "JSON_CONTAINS(JSON_EXTRACT(payload,'$.screenshot_ids'),CAST(:id AS JSON)) LIMIT 1"
            ),
            {"id": screenshot_id},
        )
    )


async def _screenshot_user_deletion_completed(session: AsyncSession, screenshot_id: int) -> bool:
    # Null feedback ownership alone never proves deletion. Match the captured screenshot,
    # committed cleanup, published callback and terminal request/account together.
    return bool(
        await session.scalar(
            text(
                "SELECT 1 FROM admin_outbox a "
                "JOIN deletion_cleanup_event e ON BINARY e.event_id=BINARY a.event_id "
                "JOIN account_deletion_request r ON BINARY r.public_id="
                "BINARY JSON_UNQUOTE(JSON_EXTRACT(a.payload,'$.deletion_request_id')) "
                "JOIN user_account u ON u.id=r.user_id "
                "WHERE a.event_type='DELETION_CLEANUP_RESULT' AND a.status='PUBLISHED' "
                "AND e.status='COMPLETED' AND r.status='DELETED' AND u.status='DELETED' "
                "AND BINARY u.public_id=BINARY JSON_UNQUOTE(JSON_EXTRACT(a.payload,'$.user_id')) "
                "AND JSON_CONTAINS(JSON_EXTRACT(a.payload,'$.screenshot_ids'),CAST(:id AS JSON)) "
                "LIMIT 1"
            ),
            {"id": screenshot_id},
        )
    )


async def _screenshot_has_references(session: AsyncSession, screenshot_id: int, key: str) -> bool:
    # Locking reads protect matching rows and insertion gaps until the delete is committed.
    # Retain every registered asset/version conservatively, not just the current publication.
    queries = (
        ("feedback_screenshot", "object_key=:key AND deleted_at IS NULL AND id<>:id"),
        ("media_asset", "object_key=:key"),
        ("content_series", "cover_object_key=:key"),
        ("scene", "cover_object_key=:key"),
        ("user_profile", "avatar_object_key=:key"),
        ("scene_revision", "JSON_SEARCH(content_snapshot,'one',:pattern,'!') IS NOT NULL"),
    )
    values = {
        "key": key,
        "id": screenshot_id,
        "pattern": key.replace("!", "!!").replace("%", "!%").replace("_", "!_"),
    }
    for table, predicate in queries:
        result = await session.execute(
            text(f"SELECT 1 FROM {table} WHERE {predicate} FOR UPDATE"), values
        )
        if result.first() is not None:
            return True
    return False


async def _audit_screenshot_cleanup(session: AsyncSession, row: Any, outcome: str) -> str:
    audit_id = new_ulid(datetime.now(UTC))
    await session.execute(
        text(
            "INSERT INTO audit_event (public_id,actor_public_id,action,object_type,"
            "object_public_id,"
            "before_summary,after_summary,request_id) VALUES "
            "(:id,'system','oss.screenshot.cleanup','feedback_screenshot',:ticket,"
            "JSON_OBJECT(),:summary,:id)"
        ),
        {
            "id": audit_id,
            "ticket": row.public_id,
            "summary": json.dumps(
                {
                    "outcome": outcome,
                    "object_key_sha256": hashlib.sha256(row.object_key.encode()).hexdigest(),
                }
            ),
        },
    )
    return audit_id


def run_cleanup_expired_drafts() -> dict[str, Any]:
    return asyncio.run(_cleanup_expired_drafts(Settings()))


async def _cleanup_expired_drafts(settings: Settings) -> dict[str, Any]:
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    repository = SQLAlchemyMediaAdminRepository(factory)
    cleaned = 0
    protected = 0
    try:
        # Seek through every due id; protected drafts cannot starve later entries.
        cursor = 0
        while True:
            async with factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id,public_id FROM draft_trash WHERE status='TRASHED' "
                            "AND retention_until<=UTC_TIMESTAMP(6) AND id>:cursor "
                            "ORDER BY id LIMIT 100"
                        ),
                        {"cursor": cursor},
                    )
                ).all()
            if not rows:
                break
            for candidate in rows:
                cursor = candidate.id
                now = datetime.now(UTC)
                try:
                    result = await repository.transition_trash(
                        candidate.public_id, "CLEANUP", "system", now
                    )
                except AppError as error:
                    if error.code not in {
                        "TRASH_ENTRY_NOT_FOUND",
                        "TRASH_ENTRY_NOT_ACTIVE",
                        "TRASH_RETENTION_ACTIVE",
                        "DRAFT_NOT_TRASHABLE",
                        "DRAFT_REFERENCED",
                    }:
                        raise
                    protected += 1
                else:
                    # A concurrent delivery may have completed this record already.
                    cleaned += int(result.cleaned_at == now)
        return {"cleaned": cleaned, "protected": protected}
    finally:
        await engine.dispose()


def run_aggregate_daily(metric_day: date | None = None) -> dict[str, Any]:
    return asyncio.run(_aggregate_daily(Settings(), metric_day=metric_day))


async def _aggregate_daily(settings: Settings, *, metric_day: date | None = None) -> dict[str, Any]:
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    day = metric_day or today - timedelta(days=1)
    if day >= today:
        raise ValueError("日事件统计只允许重算已结束的北京时间自然日")
    engine = create_engine(_database_url(settings))
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            # Read immutable events including later outcomes used for cohort-based rates.
            start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Shanghai"))
            utc_start = start.astimezone(UTC).replace(tzinfo=None)
            utc_end = (start + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None)
            rows = (
                await session.execute(
                    text(
                        "SELECT e.id,e.user_id,e.event_type,e.occurred_at,e.dimension,e.payload "
                        "FROM analytics_event e WHERE "
                        "((e.occurred_at>=:start AND e.occurred_at<:end) "
                        "OR JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.cohort_day'))=:day "
                        "OR JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.started_day'))=:day "
                        "OR JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.created_day'))=:day) "
                        "AND (e.event_type<>'CONTACT_SUBMITTED' OR e.user_id IS NULL OR NOT EXISTS "
                        "(SELECT 1 FROM analytics_event earlier WHERE "
                        "earlier.event_type='CONTACT_SUBMITTED' AND earlier.user_id=e.user_id "
                        "AND (earlier.occurred_at<e.occurred_at OR "
                        "(earlier.occurred_at=e.occurred_at AND earlier.id<e.id)))) "
                        "ORDER BY e.occurred_at,e.id"
                    ),
                    {"start": utc_start, "end": utc_end, "day": day.isoformat()},
                )
            ).all()
            events = [
                AnalyticsEvent(
                    row.id,
                    row.event_type,
                    row.occurred_at,
                    row.dimension,
                    _json_dict(row.payload),
                    contact_subject=row.user_id,
                )
                for row in rows
            ]
            buckets = aggregate_events(events, day)
            for metric in set(EVENT_METRICS.values()):
                buckets.setdefault((metric, "ALL"), 0)
            # Serialize retries/backfills of a single day; replacing rows is atomic.
            await session.execute(
                text(
                    "INSERT INTO analytics_daily "
                    "(metric_day,metric,dimension,metric_value,generated_at) "
                    "VALUES (:day,'NEW_USERS','ALL',0,UTC_TIMESTAMP(6)) "
                    "ON DUPLICATE KEY UPDATE generated_at=generated_at"
                ),
                {"day": day},
            )
            await session.execute(
                text("SELECT metric_day FROM analytics_daily WHERE metric_day=:day FOR UPDATE"),
                {"day": day},
            )
            snapshot_metrics = {
                "CONTACT_STATES",
                "FORMAL_STATES",
                "LIMITED_STATES",
                "FEEDBACK_STATES",
            }
            allowed = ",".join(
                "'" + metric + "'" for metric in EXPORTABLE_METRICS - snapshot_metrics
            )
            await session.execute(
                text(
                    f"DELETE FROM analytics_daily WHERE metric_day=:day AND metric IN ({allowed})"
                ),
                {"day": day},
            )
            for (metric, dimension), value in buckets.items():
                await session.execute(
                    text(
                        "INSERT INTO analytics_daily "
                        "(metric_day,metric,dimension,metric_value,generated_at) "
                        "VALUES (:day,:metric,:dimension,:value,UTC_TIMESTAMP(6))"
                    ),
                    {"day": day, "metric": metric, "dimension": dimension, "value": value},
                )
            # Current states are explicitly dated snapshots. Historical backfills never invent them.
            if metric_day is None:
                snapshot_queries = {
                    "CONTACT_STATES": "SELECT COALESCE(c.contact_status,'NOT_PROVIDED') AS status, "
                    "COUNT(*) AS amount FROM user_account u "
                    "LEFT JOIN user_contact c ON c.user_id=u.id "
                    "WHERE u.status<>'DELETED' GROUP BY COALESCE(c.contact_status,'NOT_PROVIDED')",
                    "FORMAL_STATES": "SELECT CASE WHEN status IN ('ACTIVE','PAUSED') "
                    "AND expires_at<=UTC_TIMESTAMP(6) THEN 'EXPIRED' "
                    "ELSE status END AS status, COUNT(*) AS amount "
                    "FROM formal_entitlement GROUP BY "
                    "CASE WHEN status IN ('ACTIVE','PAUSED') AND expires_at<=UTC_TIMESTAMP(6) "
                    "THEN 'EXPIRED' ELSE status END",
                    "LIMITED_STATES": "SELECT status,COUNT(*) AS amount "
                    "FROM limited_entitlement GROUP BY status",
                    "FEEDBACK_STATES": "SELECT status,COUNT(*) AS amount "
                    "FROM feedback_ticket GROUP BY status",
                }
                for metric, query in snapshot_queries.items():
                    await session.execute(
                        text(
                            "DELETE FROM analytics_daily WHERE metric_day=:day AND metric=:metric"
                        ),
                        {"day": today, "metric": metric},
                    )
                    snapshot_rows = (await session.execute(text(query))).all()
                    statuses = {
                        "CONTACT_STATES": (
                            "NOT_PROVIDED",
                            "PENDING",
                            "CONTACTED",
                            "UNREACHABLE",
                            "DO_NOT_CONTACT",
                        ),
                        "FORMAL_STATES": ("ACTIVE", "PAUSED", "EXPIRED", "REVOKED"),
                        "LIMITED_STATES": (
                            "PENDING",
                            "ACTIVE",
                            "PAUSED",
                            "START_EXPIRED",
                            "ENDED",
                            "REVOKED",
                        ),
                        "FEEDBACK_STATES": (
                            "PENDING",
                            "PROCESSING",
                            "NEED_MORE",
                            "USER_SUPPLIED",
                            "RESOLVED",
                            "CLOSED_INSUFFICIENT",
                        ),
                    }
                    state_counts = dict.fromkeys(statuses[metric], 0)
                    state_counts.update({row.status: row.amount for row in snapshot_rows})
                    for state, amount in state_counts.items():
                        await session.execute(
                            text(
                                "INSERT INTO analytics_daily "
                                "(metric_day,metric,dimension,metric_value,generated_at) "
                                "VALUES (:day,:metric,:dimension,:value,UTC_TIMESTAMP(6))"
                            ),
                            {
                                "day": today,
                                "metric": metric,
                                "dimension": state,
                                "value": amount,
                            },
                        )
        # Later successful outcomes update their original cohorts on the next daily run.
        # Explicit backfills stay limited to the requested day and never recurse.
        refreshed_cohort_days: list[str] = []
        if metric_day is None:
            affected: set[date] = set()
            for event in events:
                event_day = (
                    event.occurred_at.replace(tzinfo=UTC)
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .date()
                )
                if event_day != day:
                    continue
                for field in ("cohort_day", "started_day", "created_day"):
                    cohort_value = event.payload.get(field)
                    if isinstance(cohort_value, str) and date.fromisoformat(cohort_value) < day:
                        affected.add(date.fromisoformat(cohort_value))
            for cohort_day in sorted(affected):
                await _aggregate_daily(settings, metric_day=cohort_day)
                refreshed_cohort_days.append(cohort_day.isoformat())
        return {
            "day": day.isoformat(),
            "snapshot_day": today.isoformat() if metric_day is None else None,
            "metric_count": len(buckets),
            "event_count": len(events),
            "refreshed_cohort_days": refreshed_cohort_days,
        }
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
