from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.errors import AppError

EXPORTABLE_METRICS = frozenset(
    {
        "NEW_USERS",
        "ACTIVE_USERS",
        "SCENE_COMPLETIONS",
        "CONTACT_FUNNEL",
        "FORMAL_ENTITLEMENTS",
        "LIMITED_STARTS",
        "LIMITED_COMPLETIONS",
        "FAVORITES",
        "REVIEWS",
        "FEEDBACK_SLA",
        "DELETIONS",
    }
)
_FORBIDDEN_DIMENSION_MARKERS = ("user:", "wechat:", "nickname:", "juya:", "screenshot:")


@dataclass(frozen=True, slots=True)
class AnalyticsRow:
    day: date
    metric: str
    dimension: str
    value: int


class AnalyticsRepository(Protocol):
    async def query(self, start: date, end: date) -> tuple[AnalyticsRow, ...]: ...


class SQLAlchemyAnalyticsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def query(self, start: date, end: date) -> tuple[AnalyticsRow, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT metric_day, metric, dimension, metric_value "
                        "FROM analytics_daily WHERE metric_day BETWEEN :start AND :end "
                        "ORDER BY metric_day, metric, dimension"
                    ),
                    {"start": start, "end": end},
                )
            ).all()
        return tuple(
            AnalyticsRow(row.metric_day, row.metric, row.dimension, row.metric_value)
            for row in rows
        )


def export_aggregate_rows(rows: tuple[AnalyticsRow, ...]) -> tuple[dict[str, object], ...]:
    exported: list[dict[str, object]] = []
    for row in rows:
        dimension = row.dimension.lower()
        if row.metric not in EXPORTABLE_METRICS or any(
            marker in dimension for marker in _FORBIDDEN_DIMENSION_MARKERS
        ):
            raise AppError(
                "ANALYTICS_EXPORT_FORBIDDEN",
                "统计导出仅允许匿名聚合数据",
                422,
            )
        exported.append(
            {
                "day": row.day.isoformat(),
                "metric": row.metric,
                "dimension": row.dimension,
                "value": row.value,
            }
        )
    return tuple(exported)
