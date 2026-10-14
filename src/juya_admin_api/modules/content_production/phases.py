"""Conservative whole-order phases from existing checks and frozen result evidence."""

from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import text

from juya_admin_api.infrastructure.db.bounded_json import read_json

from .projection import counts
from .schemas import PhaseCounts, PhaseRetry, WorkItemDetail, WorkOrderPhases, WorkPhase

if TYPE_CHECKING:
    from .service import WorkOrderService

STAGES = {
    "parsing": {"VALIDATE"},
    "matching": {"AI_LEXICON"},
    "audio": {"AUTO_AUDIO", "ASR_BOUNDARY", "ASR_POLL", "SENTENCE_SLICE"},
    "publication": {"PUBLICATION", "PUBLISH"},
}
LABELS: dict[Literal["parsing", "matching", "audio", "publication"], str] = {
    "parsing": "分篇 / 中英文配对",
    "matching": "素材与词库匹配",
    "audio": "音频对齐与单句文件",
    "publication": "校对与发布",
}


def aggregate(
    items: list[WorkItemDetail],
    *,
    content_type: str,
    parsed: bool | None = None,
    publication_results: dict[str, str] | None = None,
    publication_selected_ids: set[str] | None = None,
) -> list[WorkPhase]:
    """Count each actual member once per phase; absence and stale tasks are not success."""
    phases = []
    for key, label in LABELS.items():
        metrics = PhaseCounts(total=len(items))
        retries: list[PhaseRetry] = []
        for item in items:
            current = [
                t
                for t in item.tasks
                if t.current
                and (
                    t.stage in STAGES[key]
                    or (key == "publication" and t.stage.startswith("PUBLICATION_RANGE_"))
                )
            ]
            if (
                key == "publication"
                and publication_selected_ids is not None
                and item.item_id not in publication_selected_ids
            ):
                current = []
            frozen = (publication_results or {}).get(item.item_id) if key == "publication" else None
            check = item.phase_checks.get(key)
            if key == "parsing" and key not in item.phase_checks:
                check = parsed
            outcome = "unknown"
            if item.production_status == "cancelled":
                outcome = "cancelled"
            elif key == "audio" and content_type != "dialogue":
                outcome = "not_applicable"
            elif key == "publication" and (
                frozen == "SUCCEEDED" or item.production_status == "published"
            ):
                outcome = "succeeded"
            elif key == "publication" and frozen == "UNKNOWN":
                outcome = "unknown"
            elif any(t.status in {"PENDING", "QUEUED", "RUNNING", "RETRY"} for t in current):
                outcome = "processing"
            elif any(t.status == "FAILED" for t in current):
                outcome = "failed"
                retries.extend(
                    PhaseRetry(item_id=item.item_id, stage_id=t.stage_id)
                    for t in current
                    if t.status == "FAILED" and t.retryable
                )
            elif key == "publication":
                outcome = (
                    "succeeded"
                    if frozen == "SUCCEEDED" or item.production_status == "published"
                    else "failed"
                    if frozen == "FAILED"
                    else "unknown"
                    if frozen == "UNKNOWN"
                    else "review_required"
                )
            elif any(
                i.correction_target
                in (
                    {"source", "content"}
                    if key == "parsing"
                    else {"entries"}
                    if key == "matching"
                    else {"audio"}
                )
                for i in item.issues
            ):
                outcome = "review_required"
            elif check is True:
                outcome = "succeeded"
            elif check is False:
                outcome = "review_required"
            setattr(metrics, outcome, getattr(metrics, outcome) + 1)
        phases.append(WorkPhase(key=key, label=label, counts=metrics, retryable=retries))
    return phases


async def read(service: "WorkOrderService", identity: str) -> WorkOrderPhases:
    """Read batch validation directly and match persisted publication results to current hashes."""
    async with service.sessions() as session:
        order = await service.row(session, identity)
    items = await service.projection.all(order)
    parsed = None
    frozen_results: dict[str, str] = {}
    publication_selected: set[str] = set()
    async with service.sessions() as session:
        if order["import_batch_id"] and order["source_mode"] in {"ZIP", "DOCX"}:
            stage = (
                (
                    await session.execute(
                        text(
                            "SELECT status,input_hash FROM content_import_stage "
                            "WHERE item_id=:batch "
                            "AND stage='VALIDATE' ORDER BY id DESC LIMIT 1"
                        ),
                        {"batch": order["import_batch_id"]},
                    )
                )
                .mappings()
                .first()
            )
            batch_hash = await session.scalar(
                text("SELECT input_hash FROM content_import_batch WHERE public_id=:batch"),
                {"batch": order["import_batch_id"]},
            )
            if stage and stage["input_hash"] == batch_hash:
                parsed = True if stage["status"] == "SUCCEEDED" else None
        if order["publication_token"]:
            candidates = (
                await read_json(
                    session,
                    "publication_range",
                    "candidates",
                    {"public_id": order["publication_token"]},
                )
                or []
            )
            result: dict[str, Any] = (
                await read_json(
                    session,
                    "publication_range",
                    "results",
                    {"public_id": order["publication_token"]},
                )
                or {}
            )
            by_revision = {c["revision_id"]: c for c in candidates}
            latest_stage = await session.scalar(
                text("SELECT stage_id FROM publication_range WHERE public_id=:id"),
                {"id": order["publication_token"]},
            )
            selected_revisions = set().union(
                *(
                    set(command.get("selected_revision_ids", []))
                    for command in result.get("commands", {}).values()
                    if command.get("stage_id") == latest_stage
                )
            )
            for item in items:
                candidate = by_revision.get(item.revision_id)
                if candidate and candidate["candidate_hash"] == item.candidate_hash:
                    status = result.get("items", {}).get(item.revision_id, {}).get("status")
                    if status:
                        frozen_results[item.item_id] = status
                    if item.revision_id in selected_revisions:
                        publication_selected.add(item.item_id)
    return WorkOrderPhases(
        work_order_id=identity,
        version=order["version"],
        counts=counts(items),
        phases=aggregate(
            items,
            content_type=order["content_type"],
            parsed=parsed,
            publication_results=frozen_results,
            publication_selected_ids=publication_selected,
        ),
    )
