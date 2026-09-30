import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Protocol

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
AnalyticsPeriod = Literal["day", "week", "month"]
ANONYMOUS_DIMENSIONS = frozenset(
    {
        "ALL",
        "NUMERATOR",
        "DENOMINATOR",
        "NOT_PROVIDED",
        "PENDING",
        "CONTACTED",
        "UNREACHABLE",
        "DO_NOT_CONTACT",
        "ACTIVE",
        "EXPIRED",
        "REVOKED",
        "CANCELLED",
        "RESOLVED",
        "PROCESSING",
        "NEED_MORE",
        "USER_SUPPLIED",
        "CLOSED_INSUFFICIENT",
        "CONTENT",
        "TECHNICAL",
        "OTHER",
        "MODE_3",
        "MODE_5",
    }
)
_CONTENT_DIMENSION = re.compile(r"(?:scene|series|package|campaign):[A-Za-z0-9_-]{1,64}\Z")
_PERSONAL_DIMENSION = re.compile(
    r"user|wechat|openid|phone|mobile|email|nickname|juya|screenshot|trajectory|wx[_-]?id",
    re.IGNORECASE,
)
RATIO_BASES = {
    "CONTACT_FUNNEL": "填写次数 / 提示曝光次数",
    "LIMITED_STARTS": "首次启动人数 / 开通人数",
    "LIMITED_COMPLETIONS": "到期前完成人数 / 首次启动人数",
    "FEEDBACK_SLA": "SLA 内处理数量 / 纳入 SLA 统计的反馈数量",
}


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
        if (
            row.metric not in EXPORTABLE_METRICS
            or not is_anonymous_dimension(row.dimension)
            or row.value < 0
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


def is_anonymous_dimension(dimension: str) -> bool:
    return dimension.upper() in ANONYMOUS_DIMENSIONS or (
        bool(_CONTENT_DIMENSION.fullmatch(dimension)) and not _PERSONAL_DIMENSION.search(dimension)
    )


def period_start(day: date, period: AnalyticsPeriod) -> date:
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return day


def query_aggregate_rows(
    rows: tuple[AnalyticsRow, ...], period: AnalyticsPeriod
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    # Use the same fail-closed privacy boundary as the existing export.
    export_aggregate_rows(rows)
    buckets: dict[tuple[date, str, str], int] = {}
    for row in rows:
        dimension = (
            row.dimension.upper()
            if row.dimension.upper() in ANONYMOUS_DIMENSIONS
            else row.dimension
        )
        key = (period_start(row.day, period), row.metric, dimension)
        buckets[key] = buckets.get(key, 0) + row.value
    counts = [
        {"day": day.isoformat(), "metric": metric, "dimension": dimension, "value": value}
        for (day, metric, dimension), value in sorted(buckets.items())
    ]
    ratios: list[dict[str, object]] = []
    pairs = {
        (day, metric)
        for day, metric, dimension in buckets
        if dimension in {"NUMERATOR", "DENOMINATOR"}
    }
    for day, metric in sorted(pairs):
        numerator = buckets.get((day, metric, "NUMERATOR"))
        denominator = buckets.get((day, metric, "DENOMINATOR"))
        if (
            metric not in RATIO_BASES
            or numerator is None
            or denominator is None
            or numerator > denominator
        ):
            raise AppError("ANALYTICS_RATIO_INVALID", "统计比率缺少有效分子、分母或口径", 422)
        ratios.append(
            {
                "day": day.isoformat(),
                "metric": metric,
                "numerator": numerator,
                "denominator": denominator,
                "rate": None if denominator == 0 else numerator / denominator,
                "basis": RATIO_BASES[metric],
            }
        )
    return counts, ratios
