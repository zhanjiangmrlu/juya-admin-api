"""Read-only measured and reserved cloud usage; estimates never become supplier invoices."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency


async def usage_summary(sessions: async_sessionmaker[AsyncSession], period: str) -> dict[str, Any]:
    async with sessions() as session:
        measured = (
            (
                await session.execute(
                    text(
                        "SELECT kind,COUNT(*) AS attempts,SUM(reserved_units) AS reserved_units,"
                        "SUM(actual_units) AS actual_units,"
                        "SUM(status='UNKNOWN') AS unknown_attempts "
                        "FROM content_cloud_attempt WHERE period=:period "
                        "GROUP BY kind ORDER BY kind"
                    ),
                    {"period": period},
                )
            )
            .mappings()
            .all()
        )
        limits = (
            (
                await session.execute(
                    text(
                        "SELECT kind,hard_limit,reserved_units FROM content_cloud_budget "
                        "WHERE period=:period ORDER BY kind"
                    ),
                    {"period": period},
                )
            )
            .mappings()
            .all()
        )
        tts = (
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS attempts,"
                        "SUM(estimated_characters) AS reserved_characters,"
                        "SUM(estimated_cost) AS estimated_microcost,"
                        "SUM(actual_characters) AS actual_characters,"
                        "SUM(actual_cost) AS verified_actual_microcost,"
                        "SUM(status='UNKNOWN') AS unknown_attempts "
                        "FROM cloud_usage_attempt WHERE period=:period"
                    ),
                    {"period": period},
                )
            )
            .mappings()
            .one()
        )
        import_counts = (
            (
                await session.execute(
                    text(
                        "SELECT status,COUNT(*) AS count FROM content_import_batch "
                        "WHERE DATE(CONVERT_TZ(created_at,'+00:00','+08:00'))=:period "
                        "GROUP BY status"
                    ),
                    {"period": period},
                )
            )
            .mappings()
            .all()
        )

        # MySQL sums are Decimal. Null indicates unmeasured/unverified, never a fabricated zero.
        def values(row: Any) -> dict[str, Any]:
            return {
                key: int(value) if value is not None and not isinstance(value, str) else value
                for key, value in row.items()
            }

        return {
            "period": period,
            "timezone": "Asia/Shanghai",
            "measured_cloud": [values(row) for row in measured],
            "budgets": [values(row) for row in limits],
            "tts": values(tts),
            "import_batches": [values(row) for row in import_counts],
            "units": {
                "AI_TOKENS": "provider_tokens",
                "ASR_MILLISECONDS": "milliseconds",
                "tts_cost": "CNY_micro_units",
            },
            "supplier_invoice_verified": False,
        }


def create_usage_router(
    sessions: async_sessionmaker[AsyncSession], *, current_admin: AdminDependency
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content", tags=["content-cloud-usage"])

    @router.get("/usage")
    async def summary(
        period: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        return await usage_summary(sessions, period)

    return router
