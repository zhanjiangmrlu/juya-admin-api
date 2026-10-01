import calendar
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
        "WEEK_ACTIVE_USERS",
        "MONTH_ACTIVE_USERS",
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
EXPORTABLE_METRICS = EXPORTABLE_METRICS | frozenset(
    {
        "SCENE_STARTS",
        "OPEN_SCENE_COMPLETIONS",
        "OPEN_ALL_COMPLETIONS",
        "OPEN_ALL_RATE",
        "OPEN_LEARNERS",
        "CONTACT_EXPOSURES",
        "CONTACT_SUBMISSIONS",
        "CONTACT_WITHDRAWALS",
        "CONTACT_STATES",
        "CONTACT_STATE_CHANGES",
        "FORMAL_STATE_CHANGES",
        "LIMITED_STATE_CHANGES",
        "FEEDBACK_STATE_CHANGES",
        "CONTACT_WITHDRAW_RATE",
        "FORMAL_STATES",
        "FORMAL_EXPIRATIONS",
        "LIMITED_GRANTS",
        "LIMITED_EXPIRATIONS",
        "LIMITED_START_EXPIRATIONS",
        "LIMITED_STATES",
        "REVISITS",
        "FEEDBACK_NEW",
        "FEEDBACK_RESPONSES",
        "FEEDBACK_RESPONSE_SECONDS",
        "FEEDBACK_SUPPLEMENTS",
        "FEEDBACK_SUPPLEMENT_ROUNDS",
        "FEEDBACK_RESOLUTIONS",
        "FEEDBACK_REOPENS",
        "FEEDBACK_TIMEOUTS",
        "FEEDBACK_STATES",
        "FEEDBACK_SOLVE_RATE",
        "FEEDBACK_REOPEN_RATE",
        "FEEDBACK_TIMEOUT_RATE",
        "DELETION_REQUESTS",
        "DELETION_WITHDRAWALS",
    }
)
AnalyticsPeriod = Literal["day", "week", "month"]
ActivityBasis = Literal["DAILY_USERS", "CALENDAR_WEEK_USERS", "CALENDAR_MONTH_USERS", "PERSON_DAYS"]
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
        "ENDED",
        "START_EXPIRED",
        "PAUSED",
        "PRONUNCIATION",
        "DISPLAY",
        "FUNCTION",
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
        "MODE_3_NUMERATOR",
        "MODE_3_DENOMINATOR",
        "MODE_5_NUMERATOR",
        "MODE_5_DENOMINATOR",
    }
)
_CONTENT_DIMENSION = re.compile(r"(?:scene|series|package|campaign):[A-Za-z0-9_-]{1,64}\Z")
_PERSONAL_DIMENSION = re.compile(
    r"user|wechat|openid|phone|mobile|email|nickname|juya|screenshot|trajectory|wx[_-]?id",
    re.IGNORECASE,
)
RATIO_BASES = {
    "FEEDBACK_RESPONSE_SECONDS": "累计首次响应秒数 / 首次响应反馈数量",
    "CONTACT_FUNNEL": "填写次数 / 提示曝光次数",
    "LIMITED_STARTS": "首次启动人数 / 开通人数",
    "LIMITED_COMPLETIONS": "到期前完成人数 / 首次启动人数",
    "LIMITED_START_EXPIRATIONS": "未开始失效人数 / 开通人数",
    "CONTACT_WITHDRAW_RATE": "撤回次数 / 填写次数",
    "OPEN_ALL_RATE": "三开放场景全部完成人数 / 开放场景启动人数",
    "FEEDBACK_SOLVE_RATE": "解决数量 / 新增反馈数量",
    "FEEDBACK_REOPEN_RATE": "重开数量 / 已解决反馈数量",
    "FEEDBACK_TIMEOUT_RATE": "超时数量 / 新增反馈数量",
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


def activity_basis(period: AnalyticsPeriod, start: date | None, end: date | None) -> ActivityBasis:
    if period == "day":
        return "DAILY_USERS"
    if start is not None and end is not None:
        if period == "week" and start.weekday() == 0 and end.weekday() == 6:
            return "CALENDAR_WEEK_USERS"
        if (
            period == "month"
            and start.day == 1
            and end.day == calendar.monthrange(end.year, end.month)[1]
        ):
            return "CALENDAR_MONTH_USERS"
    return "PERSON_DAYS"


def query_aggregate_rows(
    rows: tuple[AnalyticsRow, ...],
    period: AnalyticsPeriod,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    # Use the same fail-closed privacy boundary as the existing export.
    export_aggregate_rows(rows)
    basis = activity_basis(period, start, end)
    active_metric = {
        "CALENDAR_WEEK_USERS": "WEEK_ACTIVE_USERS",
        "CALENDAR_MONTH_USERS": "MONTH_ACTIVE_USERS",
    }.get(basis, "ACTIVE_USERS")
    buckets: dict[tuple[date, str, str], int] = {}
    snapshot_metrics = {"CONTACT_STATES", "FORMAL_STATES", "LIMITED_STATES", "FEEDBACK_STATES"}
    latest: dict[tuple[date, str], date] = {}
    for row in rows:
        if row.metric in snapshot_metrics:
            snapshot_key = (period_start(row.day, period), row.metric)
            latest[snapshot_key] = max(latest.get(snapshot_key, row.day), row.day)
    for row in rows:
        if row.metric in {"ACTIVE_USERS", "WEEK_ACTIVE_USERS", "MONTH_ACTIVE_USERS"}:
            if row.metric != active_metric:
                continue
            row = AnalyticsRow(row.day, "ACTIVE_USERS", row.dimension, row.value)
        if (
            row.metric in snapshot_metrics
            and row.day != latest[(period_start(row.day, period), row.metric)]
        ):
            continue
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
        (
            day,
            metric,
            "ALL" if dimension in {"NUMERATOR", "DENOMINATOR"} else dimension.rsplit("_", 1)[0],
        )
        for day, metric, dimension in buckets
        if dimension
        in {
            "NUMERATOR",
            "DENOMINATOR",
            "MODE_3_NUMERATOR",
            "MODE_3_DENOMINATOR",
            "MODE_5_NUMERATOR",
            "MODE_5_DENOMINATOR",
        }
    }
    for day, metric, dimension in sorted(pairs):
        prefix = "" if dimension == "ALL" else dimension + "_"
        numerator = buckets.get((day, metric, prefix + "NUMERATOR"))
        denominator = buckets.get((day, metric, prefix + "DENOMINATOR"))
        if (
            metric not in RATIO_BASES
            or numerator is None
            or denominator is None
            or (metric != "FEEDBACK_RESPONSE_SECONDS" and numerator > denominator)
        ):
            raise AppError("ANALYTICS_RATIO_INVALID", "统计比率缺少有效分子、分母或口径", 422)
        ratio = {
            "day": day.isoformat(),
            "metric": metric,
            "numerator": numerator,
            "denominator": denominator,
            "rate": None if denominator == 0 else numerator / denominator,
            "basis": RATIO_BASES[metric],
        }
        if metric == "FEEDBACK_RESPONSE_SECONDS":
            ratio["unit"] = "seconds"
        if dimension != "ALL":
            ratio["dimension"] = dimension
        ratios.append(ratio)
    return counts, ratios
