from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import CursorResult, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.cloud_usage.models import attempts, budgets
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


async def fence_within(session: AsyncSession, claim: Claim) -> None:
    valid = await session.scalar(
        text(
            "SELECT id FROM content_import_stage WHERE public_id=:id AND status='RUNNING' "
            "AND fencing_version=:fence AND lease_owner=:owner AND lease_until>:now "
            "AND input_hash=:hash AND config_version=:config FOR UPDATE"
        ),
        {
            "id": claim.id,
            "fence": claim.fencing_version,
            "owner": claim.owner,
            "now": datetime.now(UTC).replace(tzinfo=None),
            "hash": claim.input_hash,
            "config": claim.config_version,
        },
    )
    if valid is None:
        raise AppError("IMPORT_STAGE_LEASE_LOST", "阶段租约已失效", 409)


class CloudUsageStore:
    async def reserve_within(
        self,
        session: AsyncSession,
        claim: Claim,
        kind: str,
        units: int,
        limit: int,
        *,
        price_version: str = "UNVERIFIED",
        region: str = "cn-beijing",
        currency: str = "CNY",
        metadata: dict[str, Any] | None = None,
        authorized_failed_retry: bool = False,
    ) -> str:
        await fence_within(session, claim)
        if units <= 0 or limit <= 0:
            raise AppError("CLOUD_BUDGET_DISABLED", "未设置云调用硬预算", 409)
        existing = (
            (
                await session.execute(
                    select(attempts)
                    .where(attempts.c.stage_id == claim.id)
                    .order_by(attempts.c.attempt_number)
                )
            )
            .mappings()
            .all()
        )
        if existing and (
            not authorized_failed_retry or any(row["status"] != "FAILED" for row in existing)
        ):
            raise AppError("PROVIDER_OUTCOME_UNKNOWN", "此收费阶段已有调用记录,禁止重复提交", 409)
        attempt_number = max((int(row["attempt_number"]) for row in existing), default=0) + 1
        period = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        await session.execute(
            text(
                "INSERT INTO content_cloud_budget(kind,period,hard_limit,reserved_units) "
                "VALUES(:kind,:period,:limit,0) ON DUPLICATE KEY UPDATE hard_limit=:limit"
            ),
            {"kind": kind, "period": period, "limit": limit},
        )
        reserved = await session.execute(
            update(budgets)
            .where(
                budgets.c.kind == kind,
                budgets.c.period == period,
                budgets.c.reserved_units + units <= budgets.c.hard_limit,
            )
            .values(reserved_units=budgets.c.reserved_units + units)
        )
        if cast(CursorResult[Any], reserved).rowcount != 1:
            raise AppError("CLOUD_BUDGET_EXHAUSTED", "云调用硬预算不足", 429)
        identifier = new_ulid(datetime.now(UTC))
        await session.execute(
            insert(attempts).values(
                public_id=identifier,
                stage_id=claim.id,
                attempt_number=attempt_number,
                kind=kind,
                period=period,
                reserved_units=units,
                status="RESERVED",
                price_version=price_version,
                region=region,
                currency=currency,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                usage=metadata,
            )
        )
        return identifier

    async def use_reserved_within(
        self, session: AsyncSession, claim: Claim, attempt_id: str
    ) -> None:
        await fence_within(session, claim)
        row = (
            (
                await session.execute(
                    select(attempts).where(attempts.c.public_id == attempt_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["stage_id"] != claim.id
            or row["status"] != "RESERVED"
            or (row["usage"] or {}).get("input_hash") != claim.input_hash
            or (row["usage"] or {}).get("config_version") != claim.config_version
        ):
            raise AppError("PROVIDER_OUTCOME_UNKNOWN", "预留重试与当前阶段不匹配", 409)

    async def record_within(
        self,
        session: AsyncSession,
        claim: Claim,
        attempt_id: str,
        status: str,
        *,
        actual_units: int | None = None,
        usage: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        await fence_within(session, claim)
        if status not in ("SUBMITTED", "SUCCEEDED", "FAILED", "UNKNOWN"):
            raise ValueError("invalid provider outcome")
        attempt = (
            (
                await session.execute(
                    select(attempts).where(attempts.c.public_id == attempt_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise AppError("CLOUD_ATTEMPT_MISSING", "云调用尝试不存在", 409)
        if attempt["status"] == "SUCCEEDED":
            return
        if actual_units is not None and (type(actual_units) is not int or actual_units < 0):
            raise ValueError("invalid provider usage")
        merged_usage = dict(attempt["usage"] or {}) | (usage or {})
        if actual_units is not None and actual_units > attempt["reserved_units"]:
            # Record an unexpected provider overrun conservatively and stop subsequent paid calls.
            await session.execute(
                update(budgets)
                .where(budgets.c.kind == attempt["kind"], budgets.c.period == attempt["period"])
                .values(
                    reserved_units=budgets.c.reserved_units
                    + actual_units
                    - attempt["reserved_units"]
                )
            )
            merged_usage["reservation_overrun"] = True
        await session.execute(
            update(attempts)
            .where(attempts.c.public_id == attempt_id)
            .values(
                status=status,
                actual_units=actual_units,
                usage=merged_usage,
                provider_request_id=request_id,
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
