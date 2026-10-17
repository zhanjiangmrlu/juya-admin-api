"""Atomic per-result decisions; only this service's exact saved lineage permits reuse."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode, encode
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError

if TYPE_CHECKING:
    from juya_admin_api.modules.content.v15_candidates import CandidateService


def result_rows(identifier: str, payload: dict[str, Any], status: str) -> list[dict[str, Any]]:
    rows = payload.get("results", [])
    for index, row in enumerate(rows):
        row.setdefault("result_id", "result-" + snapshot_hash([identifier, index])[:32])
        row.setdefault("status", status if status in {"ADOPTED", "REJECTED"} else "PENDING")
    return list(rows)


async def decide(
    service: "CandidateService",
    identifier: str,
    decisions: list[dict[str, Any]],
    expected_version: int,
    actor: str,
    key: str,
) -> dict[str, Any]:
    from juya_admin_api.modules.content.v15_candidates import dependency

    scope = "candidate.decide:" + identifier
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
        if row is not None and row["dismissed_at"] is not None:
            raise AppError("CANDIDATE_DISMISSED", "本组推荐已放弃", 409)
        if row is None or row["status"] not in {"READY", "ADOPTED", "REJECTED", "DECIDED"}:
            raise AppError("CANDIDATE_NOT_READY", "候选未就绪", 409)
        payload = decode(row["candidate"])
        if payload.get("kind") != "AI_LEXICON":
            raise AppError("ASR_MANUAL_BOUNDARY_REQUIRED", "ASR只供人工边界核对", 409)
        revision, content = await service._draft(session, str(row["revision_id"]))
        expected_hash = payload.get("decision_input_hash") or row["input_hash"]
        if (
            revision.version != expected_version
            or dependency(content, revision.version, "AI_LEXICON") != expected_hash
            or payload["config_version"] != service.config_version
        ):
            raise AppError("CANDIDATE_INPUT_CHANGED", "草稿依赖已变化", 409)
        rows = result_rows(identifier, payload, str(row["status"]))
        by_id = {item["result_id"]: item for item in rows}
        selected_ids = [d["result_id"] for d in decisions]
        if not selected_ids or len(set(selected_ids)) != len(selected_ids):
            raise AppError("CANDIDATE_SELECTION_INVALID", "候选选择无效", 422)
        changed = False
        adopted_positions = []
        for decision in decisions:
            selected = by_id.get(decision["result_id"])
            if selected is None:
                raise AppError("CANDIDATE_INDEX_INVALID", "候选条目不存在", 422)
            if selected["status"] != "PENDING":
                raise AppError("CANDIDATE_ALREADY_DECIDED", "候选已处理", 409)
            status = decision["decision"]
            if status not in {"ADOPTED", "REJECTED"}:
                raise AppError("CANDIDATE_DECISION_INVALID", "候选处理方式无效", 422)
            if status == "ADOPTED":
                source_id = decision.get("source_id", selected["source_id"])
                start = decision.get("start", selected.get("start"))
                end = decision.get("end", selected.get("end"))
                chinese = decision.get("chinese", selected["chinese"])
                kind = decision.get("entry_type", "VOCABULARY")
                sentence = next((line for line in content.dialogue if line.id == source_id), None)
                if kind not in {"VOCABULARY", "PHRASE"}:
                    raise AppError("CANDIDATE_ENTRY_TYPE_INVALID", "词条类型无效", 422)
                if (
                    sentence is None
                    or source_id != selected["source_id"]
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                    or not 0 <= start < end <= len(sentence.english)
                    or sentence.english[start:end] != selected["english"]
                    or not chinese.strip()
                ):
                    raise AppError("CANDIDATE_SOURCE_INVALID", "请准确校正来源位置和中文", 422)
                content.schema_version = 2
                group = content.vocabulary if kind == "VOCABULARY" else content.chunks
                adopted_positions.append((selected, kind, len(group)))
                group.append(
                    SceneEntry(
                        english=selected["english"],
                        chinese=chinese,
                        source_sentence_ids=[source_id],
                        source_sentence=sentence.english,
                        source_locator={
                            "sentence_id": source_id,
                            "start": str(start),
                            "end": str(end),
                        },
                    )
                )
                changed = True
            selected.update(
                status=status, decided_by=actor, decided_at=datetime.now(UTC).isoformat()
            )
        saved = (
            await service.production.save_revision_within(
                session,
                replace(revision, content=content.model_dump(mode="json"), created_by=actor),
                expected_version,
            )
            if changed
            else revision
        )
        content = SceneContent.model_validate(saved.content)
        for selected, kind, position in adopted_positions:
            selected["entry_id"] = (content.vocabulary if kind == "VOCABULARY" else content.chunks)[
                position
            ].entry_id
        payload["decision_input_hash"] = dependency(content, saved.version, "AI_LEXICON")
        statuses = {r["status"] for r in rows}
        status = (
            "READY"
            if "PENDING" in statuses
            else next(iter(statuses))
            if len(statuses) == 1
            else "DECIDED"
        )
        await session.execute(
            text(
                "UPDATE lexicon_candidate SET candidate=:payload,status=:status,decided_by=:actor,"
                "decided_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
            ),
            dict(payload=encode(payload), status=status, actor=actor, id=identifier),
        )
        result = dict(
            candidate_id=identifier,
            revision_id=saved.id,
            content_id=saved.scene_id,
            edit_version=saved.version,
            content=saved.content,
            status=status,
            results=rows,
        )
        from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore

        await ProofreadingStore(service.sessions).audit(
            session,
            saved.id,
            actor,
            "work.ai.decide",
            dict(
                candidate_id=identifier,
                input_hash=row["input_hash"],
                config_version=payload["config_version"],
                decisions=decisions,
                candidate_hash=snapshot_hash(saved.content),
                results=rows,
            ),
        )
        await service.production.remember_creation(
            session, scope, actor, key, digest, result, datetime.now(UTC)
        )
        return result
