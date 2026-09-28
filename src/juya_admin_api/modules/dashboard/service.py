from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    active_users: int
    open_feedback: int
    overdue_feedback: int
    expiring_entitlements: int
    failed_jobs: int


class DashboardRepository(Protocol):
    async def snapshot(self) -> DashboardSnapshot: ...


class DashboardService:
    def __init__(self, repository: DashboardRepository) -> None:
        self._repository = repository

    async def get_snapshot(self) -> DashboardSnapshot:
        return await self._repository.snapshot()


class SQLAlchemyDashboardRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def snapshot(self) -> DashboardSnapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM user_admin_projection "
                        " WHERE account_status = 'ACTIVE') AS active_users, "
                        "(SELECT COUNT(*) FROM feedback_ticket WHERE status IN "
                        " ('PENDING','PROCESSING','NEED_MORE','USER_SUPPLIED')) "
                        " AS open_feedback, "
                        "(SELECT COUNT(*) FROM feedback_ticket WHERE deadline_at < "
                        " UTC_TIMESTAMP(6) "
                        " AND status IN ('PENDING','PROCESSING','USER_SUPPLIED')) "
                        " AS overdue_feedback, "
                        "(SELECT COUNT(*) FROM formal_entitlement WHERE status = 'ACTIVE' "
                        " AND expires_at BETWEEN UTC_TIMESTAMP(6) AND "
                        " DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 7 DAY)) AS expiring_entitlements, "
                        "(SELECT COUNT(*) FROM batch_job WHERE status = 'COMPLETED_WITH_ERRORS') "
                        " AS failed_jobs"
                    )
                )
            ).one()
        return DashboardSnapshot(
            active_users=row.active_users,
            open_feedback=row.open_feedback,
            overdue_feedback=row.overdue_feedback,
            expiring_entitlements=row.expiring_entitlements,
            failed_jobs=row.failed_jobs,
        )
