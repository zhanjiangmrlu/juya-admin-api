from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from juya_admin_api.modules.analytics.service import (
    ActivityBasis,
    AnalyticsPeriod,
    AnalyticsRepository,
    activity_basis,
    query_aggregate_rows,
)


class AnalyticsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: AnalyticsPeriod = "day"
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        # 功能: 校验统计查询起止日期顺序和范围.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 校验完成的当前配置或查询模型.
        if self.end < self.start or (self.end - self.start).days > 365:
            raise ValueError("日期区间须正序且最多包含 366 天")
        return self


class AnalyticsCountResponse(BaseModel):
    day: date
    metric: str
    dimension: str
    value: int = Field(ge=0)


class AnalyticsRatioResponse(BaseModel):
    day: date
    metric: str
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    rate: float | None
    basis: str
    dimension: str | None = None
    unit: Literal["ratio", "seconds"] = "ratio"


class AnalyticsResponse(BaseModel):
    period: AnalyticsPeriod
    activity_basis: ActivityBasis
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    start: date
    end: date
    rows: list[AnalyticsCountResponse]
    ratios: list[AnalyticsRatioResponse]


def create_analytics_router(
    repository: AnalyticsRepository,
    *,
    current_admin: Callable[..., Awaitable[object]],
) -> APIRouter:
    # 功能: 创建匿名运营统计查询路由.
    # 参数:
    #     repository: 提供匿名统计持久化和查询能力的仓储.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin/analytics", tags=["analytics"])

    @router.get("", response_model=AnalyticsResponse)
    async def query_analytics(
        query: Annotated[AnalyticsQuery, Query()],
        _admin: Annotated[object, Depends(current_admin)],
    ) -> AnalyticsResponse:
        # 功能: 按周期查询匿名指标和比率并返回统计口径.
        # 参数:
        #     query: 包含周期和日期范围的统计查询条件.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 统计计数,比率以及活跃人数口径的响应模型.
        counts, ratios = query_aggregate_rows(
            await repository.query(query.start, query.end),
            query.period,
            start=query.start,
            end=query.end,
        )
        return AnalyticsResponse.model_validate(
            {
                "period": query.period,
                "activity_basis": activity_basis(query.period, query.start, query.end),
                "start": query.start,
                "end": query.end,
                "rows": counts,
                "ratios": ratios,
            }
        )

    return router
