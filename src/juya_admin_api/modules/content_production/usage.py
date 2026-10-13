"""Only real attempts associated with this order; no global totals or invented costs."""

from typing import TYPE_CHECKING

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content_production.schemas import UsageAttempt, WorkOrderUsage

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService


async def usage(service: "WorkOrderService", identity: str) -> WorkOrderUsage:
    async with service.sessions() as session:
        order = await service.row(session, identity)
    await service.projection.all(order)
    async with service.sessions() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT a.* FROM content_cloud_attempt a JOIN "
                        "content_work_item_stage s "
                        "ON s.stage_id=a.stage_id JOIN content_work_item i ON "
                        "i.public_id=s.work_item_id "
                        "WHERE i.work_order_id=:id ORDER BY a.created_at,a.public_id"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .all()
        )
    attempts = []
    for row in rows:
        evidence = decode(row["usage"])
        attempts.append(
            UsageAttempt(
                attempt_id=row["public_id"],
                stage_id=row["stage_id"],
                kind=row["kind"],
                attempt_number=row["attempt_number"],
                status=row["status"],
                reserved_units=row["reserved_units"],
                actual_units=row["actual_units"],
                input_tokens=evidence.get("prompt_tokens"),
                output_tokens=evidence.get("completion_tokens"),
                measured_audio_ms=evidence.get("measured_audio_duration_ms"),
                model=evidence.get("model"),
                created_at=row["created_at"],
            )
        )
    return WorkOrderUsage(
        work_order_id=identity,
        attempts=attempts,
        unknown_count=sum(a.status == "UNKNOWN" for a in attempts),
        retry_count=sum(a.attempt_number > 1 for a in attempts),
    )
