import os
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.modules.analytics.service import AnalyticsRow
from juya_admin_api.shared.errors import install_error_handlers


class AggregateRepository:
    def __init__(self, rows: tuple[AnalyticsRow, ...]) -> None:
        # 功能:初始化 AggregateRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 AggregateRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     rows: 预设统计聚合记录,供日期筛选和指标汇总断言。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.rows = rows
        self.calls: list[tuple[date, date]] = []

    async def query(self, start: date, end: date) -> tuple[AnalyticsRow, ...]:
        # 功能:记录日期查询并筛选范围内的预设统计行。
        # 参数:
        #     self: 当前 AggregateRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     start: 统计查询起始日期。
        #     end: 统计查询结束日期。
        # 返回:指定日期范围内的统计行元组。
        self.calls.append((start, end))
        return tuple(row for row in self.rows if start <= row.day <= end)


def client_for(rows: tuple[AnalyticsRow, ...]) -> tuple[TestClient, AggregateRepository]:
    # Import in the fixture so RED proves the missing route, not collection failure.
    # 功能:组装当前用例所需路由、依赖和内存仓库的 HTTP 测试客户端。
    # 参数:
    #     rows: 预设统计聚合记录,供日期筛选和指标汇总断言。
    # 返回:tuple[TestClient, AggregateRepository],由本用例预设的数据或所组装的测试资源构成。
    from juya_admin_api.modules.analytics.router import create_analytics_router

    repository = AggregateRepository(rows)
    app = FastAPI()
    install_error_handlers(app)

    async def current_admin() -> object:
        # 功能:提供当前测试的管理员认证依赖。
        # 参数:无。
        # 返回:object,由本用例预设的数据或所组装的测试资源构成。
        return object()

    app.include_router(create_analytics_router(repository, current_admin=current_admin))
    return TestClient(app), repository


@pytest.mark.parametrize(
    ("period", "buckets"),
    [
        ("day", [("2026-09-27", 2), ("2026-09-28", 3), ("2026-10-01", 5)]),
        ("week", [("2026-09-21", 2), ("2026-09-28", 8)]),
        ("month", [("2026-09-01", 5), ("2026-10-01", 5)]),
    ],
)
def test_query_uses_shanghai_dates_and_monday_weeks(
    period: str, buckets: list[tuple[str, int]]
) -> None:
    # 功能:验证统计使用北京时间日期并以周一作为周起点。
    # 参数:
    #     period: 统计分桶周期,例如 day、week 或 month。
    #     buckets: 预期统计分桶列表,元素包含日期标签和计数。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository = client_for(
        tuple(
            AnalyticsRow(date.fromisoformat(day), "NEW_USERS", "ALL", value)
            for day, value in [("2026-09-27", 2), ("2026-09-28", 3), ("2026-10-01", 5)]
        )
    )
    response = client.get(
        "/api/v1/admin/analytics",
        params={"period": period, "start": "2026-09-27", "end": "2026-10-01"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Asia/Shanghai"
    assert body["period"] == period
    assert [(row["day"], row["value"]) for row in body["rows"]] == buckets
    assert repository.calls == [(date(2026, 9, 27), date(2026, 10, 1))]


@pytest.mark.parametrize(
    ("metric", "dimension"),
    [
        ("USER_TRACE", "ALL"),
        ("ACTIVE_USERS", "user_id:1"),
        ("NEW_USERS", "openid:secret"),
        ("NEW_USERS", "nickname:alice"),
        ("NEW_USERS", "arbitrary-secret"),
        ("NEW_USERS", "scene:user_id1"),
    ],
)
def test_query_rejects_unknown_metrics_and_non_allowlisted_dimensions(
    metric: str, dimension: str
) -> None:
    # 功能:验证统计拒绝未知指标和非白名单维度。
    # 参数:
    #     metric: 待测试的统计指标名称。
    #     dimension: 待测试的统计维度名称。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = client_for((AnalyticsRow(date(2026, 9, 30), metric, dimension, 1),))
    response = client.get("/api/v1/admin/analytics?period=day&start=2026-09-30&end=2026-09-30")
    assert response.status_code == 422
    assert "secret" not in response.text
    assert "alice" not in response.text


@pytest.mark.parametrize("denominator, expected_rate", [(0, None), (10, 0.4)])
def test_ratio_keeps_components_and_basis_without_averaging_daily_rates(
    denominator: int, expected_rate: float | None
) -> None:
    # 功能:验证比例保留分子、分母及口径且不平均每日比率。
    # 参数:
    #     denominator: 预设比率指标的分母,用于检查空值与汇总计算。
    #     expected_rate: 预期汇总比率;None 表示分母为零时不生成比率。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = client_for(
        (
            AnalyticsRow(date(2026, 9, 29), "FEEDBACK_SLA", "NUMERATOR", 1 if denominator else 0),
            AnalyticsRow(date(2026, 9, 29), "FEEDBACK_SLA", "DENOMINATOR", denominator // 2),
            AnalyticsRow(date(2026, 9, 30), "FEEDBACK_SLA", "NUMERATOR", 3 if denominator else 0),
            AnalyticsRow(date(2026, 9, 30), "FEEDBACK_SLA", "DENOMINATOR", denominator // 2),
        )
    )
    response = client.get("/api/v1/admin/analytics?period=month&start=2026-09-01&end=2026-09-30")
    assert response.status_code == 200
    ratio = response.json()["ratios"][0]
    assert ratio["day"] == "2026-09-01"
    assert ratio["numerator"] == (4 if denominator else 0)
    assert ratio["denominator"] == denominator
    assert ratio["rate"] == expected_rate
    assert ratio["basis"]


@pytest.mark.parametrize(
    "extra", ["user_id=1", "metric=UNKNOWN", "dimension=wechat:secret", "period=year"]
)
def test_unknown_query_fields_and_invalid_period_are_rejected(extra: str) -> None:
    # 功能:验证未知查询字段和无效统计周期被拒绝。
    # 参数:
    #     extra: 参数化请求附加字段,用于验证不支持字段被拒绝。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository = client_for(())
    response = client.get(f"/api/v1/admin/analytics?start=2026-09-01&end=2026-09-30&{extra}")
    assert response.status_code == 422
    assert repository.calls == []


def test_reversed_range_is_rejected_and_empty_data_is_not_fabricated() -> None:
    # 功能:验证反向日期范围被拒绝且空数据不被伪造。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository = client_for(())
    assert client.get("/api/v1/admin/analytics?start=2026-10-01&end=2026-09-01").status_code == 422
    assert repository.calls == []
    body = client.get("/api/v1/admin/analytics?start=2026-09-01&end=2026-09-30").json()
    assert body["rows"] == []
    assert body["ratios"] == []


@pytest.mark.parametrize(
    ("period", "start", "end", "basis", "value"),
    [
        ("day", "2026-09-01", "2026-09-30", "DAILY_USERS", 5),
        ("week", "2026-09-28", "2026-10-04", "CALENDAR_WEEK_USERS", 2),
        ("month", "2026-09-01", "2026-09-30", "CALENDAR_MONTH_USERS", 1),
        ("week", "2026-09-29", "2026-10-01", "PERSON_DAYS", 5),
    ],
)
def test_activity_counts_use_unique_calendar_period_or_explicit_person_days(
    period: str, start: str, end: str, basis: str, value: int
) -> None:
    # 功能:验证活跃量使用日历周期去重或明确的人日口径。
    # 参数:
    #     period: 统计分桶周期,例如 day、week 或 month。
    #     start: 统计查询起始日期。
    #     end: 统计查询结束日期。
    #     basis: 统计计算口径,如日历去重或人日计数。
    #     value: 本测试待验证的配置值或领域输入。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _ = client_for(
        (
            AnalyticsRow(date(2026, 9, 29), "ACTIVE_USERS", "ALL", 3),
            AnalyticsRow(date(2026, 9, 30), "ACTIVE_USERS", "ALL", 2),
            AnalyticsRow(date(2026, 9, 29), "WEEK_ACTIVE_USERS", "ALL", 2),
            AnalyticsRow(date(2026, 9, 29), "MONTH_ACTIVE_USERS", "ALL", 1),
        )
    )
    response = client.get(
        "/api/v1/admin/analytics", params={"period": period, "start": start, "end": end}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["activity_basis"] == basis
    assert {row["metric"] for row in body["rows"]} == {"ACTIVE_USERS"}
    assert sum(row["value"] for row in body["rows"]) == value


@pytest.mark.asyncio
async def test_sql_query_keeps_date_bounds_and_aggregates_persisted_rows() -> None:
    # 功能:验证 SQL 查询保留日期边界并汇总持久化记录。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from juya_admin_api.modules.analytics.service import (
        SQLAlchemyAnalyticsRepository,
        query_aggregate_rows,
    )

    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration")
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_async_engine(url.replace("mysql+pymysql", "mysql+asyncmy"))
    sessions = async_sessionmaker(engine)
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM analytics_daily "
                    "WHERE metric_day BETWEEN '2030-12-29' AND '2031-01-01'"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO analytics_daily "
                    "(metric_day, metric, dimension, metric_value, generated_at) VALUES "
                    "('2030-12-29','NEW_USERS','ALL',2,UTC_TIMESTAMP(6)), "
                    "('2030-12-30','NEW_USERS','ALL',3,UTC_TIMESTAMP(6)), "
                    "('2031-01-01','NEW_USERS','ALL',5,UTC_TIMESTAMP(6))"
                )
            )
        repository = SQLAlchemyAnalyticsRepository(sessions)
        rows = await repository.query(date(2030, 12, 30), date(2031, 1, 1))
        counts, _ = query_aggregate_rows(rows, "week")
        assert counts == [
            {"day": "2030-12-30", "metric": "NEW_USERS", "dimension": "ALL", "value": 8}
        ]
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM analytics_daily "
                    "WHERE metric_day BETWEEN '2030-12-29' AND '2031-01-01'"
                )
            )
        await engine.dispose()
