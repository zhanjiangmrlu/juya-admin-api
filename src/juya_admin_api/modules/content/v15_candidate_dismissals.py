"""Human abandonment is separate from provider outcome and immutable spend evidence."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.shared.errors import AppError

if TYPE_CHECKING:
    from juya_admin_api.modules.content.v15_candidates import CandidateService


async def dismiss(
    service: "CandidateService",
    identifier: str,
    expected_version: int,
    actor: str,
    key: str,
    reason: str,
) -> dict[str, Any]:
    scope = "candidate.dismiss:" + identifier
    digest = snapshot_hash([expected_version, reason])
    async with service.sessions() as session, session.begin():
        replay = await service.production.creation_replay(session, scope, actor, key, digest)
        if replay is not None:
            return replay
        row = (
            (
                await session.execute(
                    text("SELECT * FROM lexicon_candidate WHERE public_id=:id FOR UPDATE"),
                    {"id": identifier},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("CANDIDATE_NOT_FOUND", "候选不存在", 404)
        payload = decode(row["candidate"])
        if payload.get("kind") != "AI_LEXICON":
            raise AppError("CANDIDATE_DISMISS_NOT_APPLICABLE", "只可放弃可选AI推荐", 422)
        revision, _content = await service._draft(
            session, str(row["revision_id"]), require_text=False
        )
        if revision.version != expected_version:
            raise AppError("REVISION_VERSION_CONFLICT", "草稿已变化,请刷新后放弃推荐", 409)
        if row["dismissed_at"] is None:
            await session.execute(
                text(
                    "UPDATE lexicon_candidate SET dismissed_by=:actor,"
                    "dismissed_at=UTC_TIMESTAMP(6),dismissal_reason=:reason WHERE public_id=:id"
                ),
                dict(id=identifier, actor=actor, reason=reason),
            )
            await ProofreadingStore(service.sessions).audit(
                session,
                revision.id,
                actor,
                "work.ai.dismiss",
                dict(
                    candidate_id=identifier,
                    input_hash=row["input_hash"],
                    provider_status=row["status"],
                    config_version=payload.get("config_version"),
                    results=payload.get("results", []),
                    reason=reason,
                    candidate_hash=snapshot_hash(revision.content),
                ),
            )
        result = dict(
            candidate_id=identifier,
            revision_id=revision.id,
            edit_version=revision.version,
            candidate_hash=snapshot_hash(revision.content),
            status="DISMISSED",
            provider_status=row["status"],
        )
        await service.production.remember_creation(
            session, scope, actor, key, digest, result, datetime.now(UTC)
        )
        return result
