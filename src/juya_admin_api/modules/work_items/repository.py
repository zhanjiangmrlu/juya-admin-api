from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.work_items.service import WorkItemFact


class SQLAlchemyWorkItemSource:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def facts(self, now: datetime) -> tuple[WorkItemFact, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT CONCAT('feedback:', public_id) AS item_key, "
                        "CASE "
                        "WHEN deadline_at < :now THEN 'FEEDBACK_OVERDUE' "
                        "WHEN status = 'USER_SUPPLIED' THEN 'USER_SUPPLIED' "
                        "WHEN deadline_at <= DATE_ADD(:now, INTERVAL 12 HOUR) "
                        "THEN 'FEEDBACK_DUE_SOON' ELSE 'NEW_FEEDBACK' END AS item_kind, "
                        "COALESCE(deadline_at, created_at) AS due_at "
                        "FROM feedback_ticket WHERE status IN "
                        "('PENDING','PROCESSING','USER_SUPPLIED') "
                        "UNION ALL "
                        "SELECT CONCAT('campaign:', public_id), "
                        "CASE WHEN grant_ends_at < :now THEN 'CAMPAIGN_START_EXPIRED' "
                        "ELSE 'CAMPAIGN_STARTING' END, grant_ends_at "
                        "FROM limited_campaign_version WHERE status = 'OPEN' "
                        "AND grant_ends_at <= DATE_ADD(:now, INTERVAL 24 HOUR) "
                        "UNION ALL "
                        "SELECT CONCAT('limited:', public_id), "
                        "'ACTIVE_ENTITLEMENT_EXPIRING', expires_at "
                        "FROM limited_entitlement WHERE status = 'ACTIVE' "
                        "AND expires_at BETWEEN :now AND DATE_ADD(:now, INTERVAL 24 HOUR)"
                    ),
                    {"now": now},
                )
            ).all()
        return tuple(_from_row(row) for row in rows)


def _from_row(row: Any) -> WorkItemFact:
    due_at = row.due_at
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    return WorkItemFact(row.item_key, row.item_kind, due_at, False)
