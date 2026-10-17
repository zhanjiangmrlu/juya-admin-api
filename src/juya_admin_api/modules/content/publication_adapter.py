"""Production publication callbacks with frozen candidates and receipt-first recovery."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.publication_ranges import (
    Candidate,
    PublicationRangeService,
    verify_facts,
)
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore, digest
from juya_admin_api.shared.errors import AppError


def build_publication_service(
    sessions: async_sessionmaker[AsyncSession],
    stages: StageStore,
    production: ProductionStore,
    content: ContentService,
    readings: ShortReadingStore,
) -> PublicationRangeService:
    async def inspect(candidate: Candidate) -> Candidate:
        identity, revision_id = candidate["content_id"], candidate["revision_id"]
        if candidate["content_type"] == "short_reading":
            async with sessions() as session, session.begin():
                reading = await readings.row(session, identity, True)
                short_revision = await readings.revision(session, identity, revision_id, True)
                errors = await readings.inspect(session, short_revision)
                current = reading["draft_revision_id"] == revision_id
                return {
                    "content_type": "short_reading",
                    "content_id": identity,
                    "revision_id": revision_id,
                    "expected_version": reading["version"],
                    "expected_edit_version": short_revision["edit_version"],
                    "candidate_hash": digest(short_revision["content_snapshot"]),
                    "warning_codes": [],
                    "error_codes": errors,
                    "ready": current
                    and short_revision["status"] == "DRAFT"
                    and not errors
                    and short_revision["content_hash"]
                    == digest(short_revision["content_snapshot"]),
                }
        revision = await content.get_revision(revision_id)
        scene = await content.get_scene(identity)
        snapshot = SceneContent.model_validate(revision.content)
        template = "word_card" if snapshot.template_type == "vocabulary" else snapshot.template_type
        scene_template = "word_card" if scene.template_type == "vocabulary" else scene.template_type
        if (
            revision.scene_id != identity
            or template != candidate["content_type"]
            or scene_template != candidate["content_type"]
        ):
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "候选类型或内容归属不匹配", 409)
        summary = await content.inspect_publish(
            revision_id, frozenset(candidate.get("warning_codes", []))
        )
        return {
            "content_type": template,
            "content_id": identity,
            "revision_id": revision_id,
            "expected_version": revision.version,
            "candidate_hash": snapshot_hash(revision.content),
            "warning_codes": list(summary.warning_codes),
            "error_codes": list(summary.error_codes),
            "ready": scene.draft_revision_id == revision_id
            and revision.status in {"DRAFT", "REVIEWED", "PUBLISH_READY"}
            and summary.ready,
        }

    async def publish(candidate: Candidate, actor: str, key: str) -> Candidate:
        if candidate["content_type"] == "short_reading":
            return await readings.command(
                "publish",
                candidate["content_id"],
                actor,
                key,
                {
                    "revision_id": candidate["revision_id"],
                    "expected_version": candidate["expected_version"],
                    "expected_edit_version": candidate["expected_edit_version"],
                    "candidate_hash": candidate["candidate_hash"],
                    "manual_confirmed": True,
                },
            )
        identity = (
            f"{candidate['revision_id']}:{candidate['expected_version']}:"
            f"{candidate['candidate_hash']}"
        )
        request_hash = hashlib.sha256(identity.encode()).hexdigest()
        async with sessions() as session:
            receipt = await production.creation_replay(
                session, "content.publish", actor, key, request_hash
            )
        if receipt is not None:
            return receipt
        facts = await inspect(candidate)
        verify_facts(candidate, facts)
        await content.validate_publish(
            candidate["revision_id"], frozenset(candidate["warning_codes"])
        )
        revision = await content.get_revision(candidate["revision_id"])
        result = await production.publish(
            replace(revision, version=candidate["expected_version"]),
            actor,
            key,
            datetime.now(UTC),
            expected_candidate_hash=candidate["candidate_hash"],
        )
        return {
            "scene_id": result.scene_id,
            "revision_id": result.revision_id,
            "published_at": result.published_at.isoformat(),
        }

    return PublicationRangeService(sessions, stages, inspect, publish)
