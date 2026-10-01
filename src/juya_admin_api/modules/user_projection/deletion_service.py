import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class DeletionCleanup:
    event_id: str
    user_id: str
    status: str
    completed_at: datetime


class DeletionRepository(Protocol):
    async def cleanup(
        self,
        event_id: str,
        user_id: str,
        now: datetime,
        *,
        deletion_request_id: str | None = None,
    ) -> DeletionCleanup: ...


class InMemoryDeletionRepository:
    def __init__(self) -> None:
        self.results: dict[str, DeletionCleanup] = {}
        self.callback_requests: dict[str, str] = {}
        self.user_formal: dict[str, set[str]] = {}
        self.user_limited: dict[str, set[str]] = {}
        self.user_feedback: dict[str, set[str]] = {}
        self.user_screenshots: dict[str, set[str]] = {}
        self.formal_entitlements: dict[str, str] = {}
        self.limited_entitlements: dict[str, str] = {}
        self.feedback_users: dict[str, str | None] = {}
        self.screenshot_deletions: set[str] = set()
        self.audit_subjects: dict[str, str] = {}

    def seed_user(
        self,
        user_id: str,
        *,
        formal_entitlements: set[str],
        limited_entitlements: set[str],
        feedback_ids: set[str],
        screenshot_keys: set[str],
    ) -> None:
        self.user_formal[user_id] = set(formal_entitlements)
        self.user_limited[user_id] = set(limited_entitlements)
        self.user_feedback[user_id] = set(feedback_ids)
        self.user_screenshots[user_id] = set(screenshot_keys)
        self.formal_entitlements.update(dict.fromkeys(formal_entitlements, "ACTIVE"))
        self.limited_entitlements.update(dict.fromkeys(limited_entitlements, "ACTIVE"))
        self.feedback_users.update(dict.fromkeys(feedback_ids, user_id))
        self.audit_subjects[user_id] = user_id

    async def cleanup(
        self,
        event_id: str,
        user_id: str,
        now: datetime,
        *,
        deletion_request_id: str | None = None,
    ) -> DeletionCleanup:
        existing = self.results.get(event_id)
        if existing is not None:
            if existing.user_id != user_id or (
                deletion_request_id is not None
                and self.callback_requests.get(event_id) != deletion_request_id
            ):
                raise AppError("DELETION_EVENT_CONFLICT", "注销事件已用于其他请求", 409)
            return existing
        for entitlement_id in self.user_formal.get(user_id, set()):
            self.formal_entitlements[entitlement_id] = "REVOKED"
        for entitlement_id in self.user_limited.get(user_id, set()):
            self.limited_entitlements[entitlement_id] = "REVOKED"
        for feedback_id in self.user_feedback.get(user_id, set()):
            self.feedback_users[feedback_id] = None
        self.screenshot_deletions.update(self.user_screenshots.get(user_id, set()))
        self.audit_subjects[user_id] = "ANONYMIZED"
        result = DeletionCleanup(event_id, user_id, "COMPLETED", now)
        self.results[event_id] = result
        if deletion_request_id is not None:
            self.callback_requests[event_id] = deletion_request_id
        return result


class DeletionCleanupService:
    def __init__(self, repository: DeletionRepository) -> None:
        self._repository = repository

    async def cleanup(
        self,
        event_id: str,
        user_id: str,
        now: datetime,
        *,
        deletion_request_id: str | None = None,
    ) -> DeletionCleanup:
        return await self._repository.cleanup(
            event_id, user_id, now, deletion_request_id=deletion_request_id
        )


class SQLAlchemyDeletionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def cleanup(
        self,
        event_id: str,
        user_id: str,
        now: datetime,
        *,
        deletion_request_id: str | None = None,
    ) -> DeletionCleanup:
        user_hash = hashlib.sha256(user_id.encode()).hexdigest()
        async with self._session_factory() as session, session.begin():
            # Serialize all cleanup requests for this user before checking replay.
            user = (
                await session.execute(
                    text("SELECT id,status FROM user_account WHERE public_id=:id FOR UPDATE"),
                    {"id": user_id},
                )
            ).first()
            if user is None:
                raise AppError("USER_NOT_FOUND", "用户不存在", 404)
            user_internal_id = user.id
            if deletion_request_id is not None:
                if len(event_id) != 26:
                    raise AppError("DELETION_EVENT_INVALID", "注销事件标识无效", 422)
                request = (
                    await session.execute(
                        text(
                            "SELECT status FROM account_deletion_request "
                            "WHERE public_id=:id AND user_id=:user FOR UPDATE"
                        ),
                        {"id": deletion_request_id, "user": user_internal_id},
                    )
                ).first()
                if (
                    request is None
                    or request.status not in {"DELETING", "DELETED"}
                    or user.status not in {"DELETING", "DELETED"}
                ):
                    raise AppError(
                        "DELETION_REQUEST_INVALID", "注销请求尚未执行或不属于该用户", 409
                    )
            replay = (
                await session.execute(
                    text(
                        "SELECT user_public_id_hash, status, completed_at "
                        "FROM deletion_cleanup_event WHERE event_id = :event_id FOR UPDATE"
                    ),
                    {"event_id": event_id},
                )
            ).first()
            if replay is not None:
                if replay.user_public_id_hash != user_hash:
                    raise AppError(
                        "DELETION_EVENT_CONFLICT",
                        "注销事件已用于其他用户",
                        409,
                    )
                if deletion_request_id is not None:
                    callback = (
                        await session.execute(
                            text(
                                "SELECT event_type,aggregate_public_id FROM admin_outbox "
                                "WHERE event_id=:id FOR UPDATE"
                            ),
                            {"id": event_id},
                        )
                    ).first()
                    if (
                        callback is None
                        or callback.event_type != "DELETION_CLEANUP_RESULT"
                        or callback.aggregate_public_id != deletion_request_id
                    ):
                        raise AppError("DELETION_EVENT_CONFLICT", "注销事件已用于其他请求", 409)
                completed_at = replay.completed_at
                if completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=UTC)
                return DeletionCleanup(event_id, user_id, replay.status, completed_at)

            screenshot_rows = (
                await session.execute(
                    text(
                        "SELECT fs.id FROM feedback_screenshot fs "
                        "JOIN feedback_ticket ft ON ft.id = fs.ticket_id "
                        "WHERE ft.user_id = :user_id AND fs.deleted_at IS NULL FOR UPDATE"
                    ),
                    {"user_id": user_internal_id},
                )
            ).all()
            await session.execute(
                text(
                    "UPDATE formal_entitlement SET status = 'REVOKED', updated_at = :now "
                    "WHERE user_id = :user_id AND status <> 'REVOKED'"
                ),
                {"user_id": user_internal_id, "now": now},
            )
            await session.execute(
                text(
                    "UPDATE limited_entitlement SET status = 'REVOKED', updated_at = :now "
                    "WHERE user_id = :user_id AND status <> 'REVOKED'"
                ),
                {"user_id": user_internal_id, "now": now},
            )
            await session.execute(
                text(
                    "UPDATE feedback_screenshot fs JOIN feedback_ticket ft "
                    "ON ft.id = fs.ticket_id SET fs.delete_after = :now "
                    "WHERE ft.user_id = :user_id AND fs.deleted_at IS NULL"
                ),
                {"user_id": user_internal_id, "now": now},
            )
            await session.execute(
                text("UPDATE feedback_ticket SET user_id = NULL WHERE user_id = :user_id"),
                {"user_id": user_internal_id},
            )
            await session.execute(
                text(
                    "UPDATE audit_event SET object_public_id = :anonymous "
                    "WHERE object_type = 'USER' AND object_public_id = :user_id"
                ),
                {"anonymous": f"deleted:{user_hash[:32]}", "user_id": user_id},
            )
            await session.execute(
                text("DELETE FROM user_admin_projection WHERE user_id = :user_id"),
                {"user_id": user_internal_id},
            )
            await session.execute(
                text(
                    "INSERT INTO deletion_cleanup_event "
                    "(event_id, user_public_id_hash, status, screenshot_delete_count, "
                    "created_at, completed_at) VALUES "
                    "(:event_id, :user_hash, 'COMPLETED', :count, :now, :now)"
                ),
                {
                    "event_id": event_id,
                    "user_hash": user_hash,
                    "count": len(screenshot_rows),
                    "now": now,
                },
            )
            if deletion_request_id is not None:
                # Same transaction as cleanup: never lose the callback on process death.
                await session.execute(
                    text(
                        "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,"
                        "payload,status,next_attempt_at,created_at) VALUES "
                        "(:event,'DELETION_CLEANUP_RESULT',:request,:payload,'PENDING',:now,:now)"
                    ),
                    {
                        "event": event_id,
                        "request": deletion_request_id,
                        "now": now,
                        "payload": json.dumps(
                            {
                                "user_id": user_id,
                                "deletion_request_id": deletion_request_id,
                                "succeeded": True,
                                "screenshot_ids": [row.id for row in screenshot_rows],
                            }
                        ),
                    },
                )
        return DeletionCleanup(event_id, user_id, "COMPLETED", now)
