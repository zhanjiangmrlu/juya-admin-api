from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.user_projection.repository import OPEN_COMPLETIONS


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    active_users: int
    open_feedback: int
    overdue_feedback: int
    expiring_entitlements: int
    failed_jobs: int
    new_users_today: int = 0
    open_completed_without_contact: int = 0
    pending_contacts: int = 0
    limited_pending: int = 0
    limited_learning: int = 0
    urgent_feedback: int = 0
    entitlement_warning_days: int = 30


class DashboardRepository(Protocol):
    async def snapshot(self) -> DashboardSnapshot: ...


class DashboardService:
    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    async def get_snapshot(self) -> DashboardSnapshot:
        return await self._repository.snapshot()


class SQLAlchemyDashboardRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        warning_days_provider: Callable[[], Awaitable[int]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._warning_days_provider = warning_days_provider
        self._clock = clock

    async def snapshot(self) -> DashboardSnapshot:
        now = self._clock()
        start = (now + timedelta(hours=8)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(hours=8)
        warning_days = await self._warning_days_provider() if self._warning_days_provider else 30
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT (SELECT COUNT(*) FROM user_account WHERE status='ACTIVE') "
                            "AS active_users, "
                            "(SELECT COUNT(*) FROM user_account WHERE created_at>=:start AND "
                            "created_at<:end) AS new_users_today, "
                            "(SELECT COUNT(*) FROM user_account u LEFT JOIN user_contact c ON "
                            "c.user_id=u.id "
                            "WHERE u.status='ACTIVE' AND c.wechat_id_ciphertext IS NULL AND "
                            f"{OPEN_COMPLETIONS}>=3) AS open_completed_without_contact, "
                            "(SELECT COUNT(*) FROM user_contact c JOIN user_account u ON "
                            "u.id=c.user_id WHERE c.contact_status='PENDING' AND "
                            "u.status='ACTIVE') AS pending_contacts, "
                            "(SELECT COUNT(*) FROM limited_entitlement WHERE status='PENDING' "
                            "AND start_deadline>:now) AS limited_pending, "
                            "(SELECT COUNT(*) FROM limited_entitlement WHERE status='ACTIVE' "
                            "AND expires_at>:now) AS limited_learning, "
                            "(SELECT COUNT(*) FROM feedback_ticket WHERE status IN "
                            "('PENDING','PROCESSING','NEED_MORE','USER_SUPPLIED')) AS "
                            "open_feedback, "
                            "(SELECT COUNT(*) FROM feedback_ticket WHERE status IN "
                            "('PENDING','PROCESSING','USER_SUPPLIED') AND deadline_at<:now) "
                            "AS overdue_feedback, "
                            "(SELECT COUNT(*) FROM feedback_ticket WHERE "
                            "status='USER_SUPPLIED' OR (status IN ('PENDING','PROCESSING') "
                            "AND deadline_at<=:due_soon)) AS urgent_feedback, "
                            "(SELECT COUNT(*) FROM formal_entitlement WHERE status='ACTIVE' "
                            "AND expires_at>:now AND expires_at<=:warning) AS "
                            "expiring_entitlements, "
                            "(SELECT COUNT(*) FROM batch_job WHERE "
                            "status='COMPLETED_WITH_ERRORS') AS failed_jobs"
                        ),
                        {
                            "now": now,
                            "start": start,
                            "end": start + timedelta(days=1),
                            "due_soon": now + timedelta(hours=12),
                            "warning": now + timedelta(days=warning_days),
                        },
                    )
                )
                .mappings()
                .one()
            )
        return DashboardSnapshot(**dict(row), entitlement_warning_days=warning_days)
