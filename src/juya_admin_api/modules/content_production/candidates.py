"""Work-order membership around persisted provided/central and AI result decisions."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode, encode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_candidate_decisions import result_rows
from juya_admin_api.modules.content.v15_candidates import dependency
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_production.lexicon import add_definition, provided_hash
from juya_admin_api.modules.content_production.schemas import (
    CandidateGroup,
    CandidateGroups,
    CandidateResult,
)
from juya_admin_api.shared.errors import AppError

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService


async def groups(service: "WorkOrderService", identity: str) -> CandidateGroups:
    async with service.sessions() as session:
        order = await service.row(session, identity)
    items = await service.projection.all(order)
    output = []
    async with service.sessions() as session:
        for item in items:
            if item.current is None or item.current.template == "short_reading":
                continue
            content = SceneContent.model_validate(item.current.content.model_dump())
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM lexicon_candidate WHERE rev"
                            "ision_id=:id AND status<>'SUPERSEDED' AND "
                            "JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.kind')) IN "
                            "('AI_LEXICON','PROVIDED_LEXICON') "
                            "ORDER BY id DESC"
                        ),
                        {"id": item.revision_id},
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                payload = decode(row["candidate"])
                kind = payload["kind"]
                results = result_rows(row["public_id"], payload, row["status"])
                actual = (
                    provided_hash(content)
                    if kind == "PROVIDED_LEXICON"
                    else dependency(content, item.edit_version or 1, "AI_LEXICON")
                )
                output.append(
                    CandidateGroup(
                        candidate_id=row["public_id"],
                        item_id=item.item_id,
                        revision_id=row["revision_id"],
                        expected_edit_version=item.edit_version or 1,
                        kind=kind,
                        status=row["status"],
                        stale=(
                            actual != (payload.get("decision_input_hash") or row["input_hash"])
                            or (
                                kind == "AI_LEXICON"
                                and payload.get("config_version")
                                != service.candidates.config_version
                            )
                        ),
                        results=[
                            CandidateResult.model_validate(
                                {
                                    k: v
                                    for k, v in result.items()
                                    if k in CandidateResult.model_fields
                                }
                            )
                            for result in results
                        ],
                    )
                )
    return CandidateGroups(work_order_id=identity, groups=output)


async def decide_provided(
    service: "WorkOrderService",
    identifier: str,
    decisions: list[dict[str, Any]],
    expected_version: int,
    actor: str,
    key: str,
) -> dict[str, Any]:
    scope = "provided.decide:" + identifier
    digest = snapshot_hash([decisions, expected_version])
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
        if not row:
            raise AppError("CANDIDATE_NOT_FOUND", "候选不存在", 404)
        if row["status"] == "SUPERSEDED":
            raise AppError("CANDIDATE_SUPERSEDED", "候选选项已刷新,请使用当前候选组", 409)
        payload = decode(row["candidate"])
        revision, content = await service.candidates._draft(
            session, str(row["revision_id"]), require_text=False
        )
        if (
            payload.get("kind") != "PROVIDED_LEXICON"
            or revision.version != expected_version
            or provided_hash(content) != (payload.get("decision_input_hash") or row["input_hash"])
        ):
            raise AppError("CANDIDATE_INPUT_CHANGED", "本篇来源或词表已变化", 409)
        results = {r["result_id"]: r for r in payload["results"]}
        if len({d["result_id"] for d in decisions}) != len(decisions):
            raise AppError("CANDIDATE_SELECTION_INVALID", "候选选择不能重复", 422)
        changed = False
        links = []
        for decision in decisions:
            result = results.get(decision["result_id"])
            if result is None or result["status"] != "PENDING":
                raise AppError("CANDIDATE_ALREADY_DECIDED", "候选不存在或已处理", 409)
            entry = next(
                (
                    e
                    for e in [*content.vocabulary, *content.chunks]
                    if e.entry_id == result["entry_id"]
                ),
                None,
            )
            if not entry:
                raise AppError("CANDIDATE_INPUT_CHANGED", "本篇条目已变化", 409)
            action = decision["decision"]
            if action not in {"KEEP_PROVIDED", "USE_CENTRAL", "REJECTED"}:
                raise AppError(
                    "CANDIDATE_DECISION_INVALID", "请选择保留上传释义或明确中央义项", 422
                )
            options = result["central_options"]
            option = next(
                (
                    o
                    for o in options
                    if o["lexicon_entry_id"] == decision.get("lexicon_entry_id")
                    and o["definition_version"] == decision.get("definition_version")
                ),
                None,
            )
            central_action = decision.get("central_action", "none")
            if action == "REJECTED":
                # Remove only this optional/current local entry; structural checks still enforce
                # at least one principal word on a word-card template.
                content.vocabulary = [e for e in content.vocabulary if e.entry_id != entry.entry_id]
                content.chunks = [e for e in content.chunks if e.entry_id != entry.entry_id]
                changed = True
            else:
                link = None
                if action == "USE_CENTRAL":
                    if not option:
                        raise AppError(
                            "CENTRAL_SENSE_REQUIRED", "请选择此候选中的明确中央义项", 422
                        )
                    active = await session.scalar(
                        text(
                            "SELECT a.version FROM lexicon_entry e "
                            "JOIN lexicon_active_sense a ON a.entry_id=e.public_id "
                            "WHERE e.public_id=:id AND a.version=:version FOR UPDATE"
                        ),
                        dict(id=option["lexicon_entry_id"], version=option["definition_version"]),
                    )
                    if not active:
                        raise AppError("CENTRAL_DEFINITION_STALE", "中央义项已变化,请重新整理", 409)
                    definition = option["content"]
                    if entry.chinese != definition["chinese"]:
                        entry.meaning_change = "new_sense"
                    entry.chinese, entry.explanation, entry.phonetic = (
                        definition["chinese"],
                        definition["explanation"],
                        definition["phonetic"],
                    )
                    changed = True
                    link = option["lexicon_entry_id"], option["definition_version"]
                elif central_action != "none":
                    if option is None and len({o["lexicon_entry_id"] for o in options}) == 1:
                        option = options[0] if central_action == "new_sense" else None
                    link = await add_definition(
                        session, entry, result["entry_type"], actor, central_action, option
                    )
                else:
                    exact = [
                        o
                        for o in options
                        if not o["meaning_differs"]
                        and o["content"]["explanation"] == entry.explanation
                    ]
                    if len(exact) == 1:
                        option = exact[0]
                        active = await session.scalar(
                            text(
                                "SELECT a.version FROM lexicon_entry e "
                                "JOIN lexicon_active_sense a ON a.entry_id=e.public_id "
                                "WHERE e.public_id=:id AND a.version=:version FOR UPDATE"
                            ),
                            dict(
                                id=option["lexicon_entry_id"], version=option["definition_version"]
                            ),
                        )
                        if not active:
                            raise AppError(
                                "CENTRAL_DEFINITION_STALE", "中央义项已变化,请重新整理", 409
                            )
                        link = option["lexicon_entry_id"], option["definition_version"]
                links.append(
                    dict(
                        central=link[0] if link else None,
                        version=link[1] if link else None,
                        id=entry.entry_id,
                        content=revision.scene_id,
                    ),
                )
            result.update(
                status="REJECTED" if action == "REJECTED" else "ADOPTED",
                decision=action,
                central_action=central_action,
                decided_by=actor,
            )
        if changed:
            revision = await service.production.save_revision_within(
                session,
                replace(revision, content=content.model_dump(mode="json"), created_by=actor),
                expected_version,
            )
        for link_update in links:
            await session.execute(
                text(
                    "UPDATE content_entry SET lexicon_entry_id=:central,"
                    "lexicon_entry_version=:version WHERE public_id=:id AND content_id=:content"
                ),
                link_update,
            )
        payload["decision_input_hash"] = provided_hash(
            SceneContent.model_validate(revision.content)
        )
        states = {r["status"] for r in payload["results"]}
        status = "READY" if "PENDING" in states else "DECIDED"
        await session.execute(
            text(
                "UPDATE lexicon_candidate SET candidate=:payload,status=:status,"
                "decided_by=:actor,decided_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
            ),
            dict(id=identifier, payload=encode(payload), status=status, actor=actor),
        )
        # Saving an explicitly changed definition can materialize an automatic replacement
        # group. This decision owns that exact new dependency, so retain just its audit lineage.
        await session.execute(
            text(
                "UPDATE lexicon_candidate SET status='SUPERSEDED' WHERE revision_id=:revision "
                "AND public_id<>:id AND input_hash=:hash AND "
                "JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.kind'))='PROVIDED_LEXICON'"
            ),
            dict(id=identifier, revision=revision.id, hash=payload["decision_input_hash"]),
        )
        await service.proofreading.audit(
            session,
            revision.id,
            actor,
            "work.lexicon.decide",
            dict(
                candidate_id=identifier,
                input_hash=row["input_hash"],
                decisions=decisions,
                candidate_hash=snapshot_hash(revision.content),
                results=payload["results"],
            ),
        )
        receipt = dict(
            revision_id=revision.id,
            edit_version=revision.version,
            candidate_hash=snapshot_hash(revision.content),
        )
        await service.production.remember_creation(
            session, scope, actor, key, digest, receipt, datetime.now(UTC)
        )
        return receipt
