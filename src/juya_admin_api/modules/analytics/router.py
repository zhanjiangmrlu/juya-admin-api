from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from juya_admin_api.modules.analytics.service import (
    AnalyticsPeriod,
    AnalyticsRepository,
    query_aggregate_rows,
)


class AnalyticsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: AnalyticsPeriod = "day"
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
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


class AnalyticsResponse(BaseModel):
    period: AnalyticsPeriod
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
    router = APIRouter(prefix="/api/v1/admin/analytics", tags=["analytics"])

    @router.get("", response_model=AnalyticsResponse)
    async def query_analytics(
        query: Annotated[AnalyticsQuery, Query()],
        _admin: Annotated[object, Depends(current_admin)],
    ) -> AnalyticsResponse:
        counts, ratios = query_aggregate_rows(
            await repository.query(query.start, query.end), query.period
        )
        return AnalyticsResponse.model_validate(
            {
                "period": query.period,
                "start": query.start,
                "end": query.end,
                "rows": counts,
                "ratios": ratios,
            }
        )

    return router
