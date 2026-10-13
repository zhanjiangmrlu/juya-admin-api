"""Recover actual local preparation and optional paid enqueue before mutable checks."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_auto_audio import schedule_audio_within
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_production.lexicon import organize_within

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService


async def organize(
    service: "WorkOrderService",
    items: list[dict[str, Any]],
    content_type: str,
    payload: dict[str, Any],
    actor: str,
    key: str,
) -> list[str]:
    digest = snapshot_hash([items, content_type, payload])
    async with service.sessions() as session, session.begin():
        prepared = await service.production.creation_replay(
            session, "work.organize", actor, key, digest
        )
        if prepared is None:
            prepared = {"stages": [], "recommendations": []}
            for item in items:
                if not item["current"]:
                    continue
                candidate = dict(
                    content_type=content_type,
                    content_id=item["content_id"],
                    revision_id=item["revision_id"],
                    candidate_hash=item["candidate_hash"],
                )
                current = await service.proofreading.current(session, candidate)
                if content_type == "short_reading":
                    await service.proofreading.facts(
                        session, content_type, item["revision_id"], current
                    )
                    continue
                body = SceneContent.model_validate(current)
                await organize_within(session, item["revision_id"], item["content_id"], body, actor)
                stage = await schedule_audio_within(
                    session, service.sessions, item["revision_id"], body
                )
                if stage:
                    prepared["stages"].append(stage)
                if (
                    payload.get("include_recommendations")
                    and not body.vocabulary
                    and not body.chunks
                ):
                    prepared["recommendations"].append(
                        dict(revision_id=item["revision_id"], edit_version=item["edit_version"])
                    )
            await service.production.remember_creation(
                session, "work.organize", actor, key, digest, prepared, datetime.now(UTC)
            )
    stages = list(prepared["stages"])
    for item in prepared["recommendations"]:
        stages.append(
            await service.candidates.queue(
                item["revision_id"],
                "AI_LEXICON",
                item["edit_version"],
                actor,
                datetime.now(UTC),
                idempotency_key=snapshot_hash([key, item["revision_id"]]),
            )
        )
    return stages
