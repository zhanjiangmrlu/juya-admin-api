"""Read authoritative drafts and tasks without scheduling provider work."""

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_auto_audio import audio_input_hash
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.modules.content_production.schemas import (
    Counts,
    ProductionStatus,
    ReadingCandidate,
    SceneCandidate,
    WorkIssue,
    WorkItemDetail,
    WorkTask,
)
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

from .effective_issues import effective_history, issue_id

READING_ISSUES = {
    "TITLE_EN_REQUIRED": ("title_en", "请填写英文标题"),
    "TITLE_ZH_REQUIRED": ("title_zh", "请填写中文标题"),
    "CATEGORY_REQUIRED": ("category", "请填写阅读类别"),
    "TOPIC_REQUIRED": ("topic", "请填写阅读题材"),
    "LEVEL_REQUIRED": ("level", "请填写阅读难度"),
    "SAFE_INTRO_REQUIRED": ("safe_intro", "请填写安全简介"),
    "TRANSLATION_ZH_REQUIRED": ("translation_zh", "请补充完整中文翻译"),
    "PARAGRAPHS_REQUIRED": ("paragraphs_en", "请补充完整英文正文"),
    "COPYRIGHT_REQUIRED": ("copyright_source", "请填写来源与版权说明并确认版权"),
}


def issues(
    values: list[dict[str, Any]], *, historical: bool = False, source_missing: bool = False
) -> list[WorkIssue]:
    result = []
    for value in values:
        code = value.get("code", "CONTENT_REVIEW_REQUIRED")
        default_field, default_message = READING_ISSUES.get(code, ("", "请核对当前内容与原始输入"))
        field = str(value.get("field", default_field))
        locations = [str(v) for v in value.get("source_locations", [])]
        if field:
            locations.append(field)
        target: Literal["content", "source", "audio", "entries", "publication"] = (
            "source"
            if source_missing
            else "audio"
            if any(s in code for s in ("AUDIO", "SLICE", "ASR", "ALIGNMENT"))
            else "entries"
            if any(s in code for s in ("ENTRY", "LEXICON"))
            else "content"
        )
        result.append(
            WorkIssue(
                id=issue_id(value),
                code=code,
                message=value.get("message") or default_message,
                location=locations,
                correction_target=target,
                historical=historical,
                origin_work_item_id=value.get("origin_work_item_id"),
                source_hash=value.get("source_hash"),
                severity=value.get("severity"),
            )
        )
    return result


def counts(items: list[WorkItemDetail]) -> Counts:
    result = Counts(total=len(items))
    for item in items:
        field = item.production_status
        # Published is a lifecycle fact as well as the category of an unchanged published candidate.
        if field != "published":
            setattr(result, field, getattr(result, field) + 1)
        result.published += int(item.published)
        result.confirmed += int(item.confirmed)
        result.publishable += int(item.publishable)
    return result


def select_items(items: list[WorkItemDetail], payload: dict[str, Any]) -> list[WorkItemDetail]:
    ids = payload.get("selected_item_ids", [])
    if len(ids) != len(set(ids)):
        raise AppError("WORK_SELECTION_INVALID", "篇目选择不能重复", 422)
    by_id = {item.item_id: item for item in items}
    if set(ids) - by_id.keys():
        raise AppError("WORK_SELECTION_INVALID", "篇目不属于此工作单", 422)
    scope = payload.get("scope", "all")
    if scope == "selected" and not ids:
        raise AppError("WORK_SELECTION_INVALID", "请选择篇目", 422)
    chosen = [by_id[i] for i in ids] if ids else items
    if scope in {"normal", "failed"}:
        chosen = [item for item in chosen if item.production_status == scope]
    elif scope in {"confirmed", "publishable"}:
        chosen = [item for item in chosen if getattr(item, scope)]
    return [item for item in chosen if item.production_status != "cancelled"]


class WorkProjection:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], proofreading: ProofreadingStore):
        self.sessions, self.proofreading = sessions, proofreading

    async def sync_imports(self, session: AsyncSession, order: dict[str, Any]) -> None:
        batch_id = order["import_batch_id"]
        if not batch_id:
            return
        if order["source_mode"] == "MATERIAL_ZIP":
            await self.sync_materials(session, order)
            return
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT i.public_id,i.row_no,i.content_id,i.revision_id,"
                        "w.content_id AS work_content_id,w.original_revision_id AS "
                        "work_original_revision,"
                        "w.public_id AS "
                        "work_item_id FROM content_import_item i LEFT JOIN content_work_item w "
                        "ON w.import_item_id=i.public_id AND w.work_order_id=:order WHERE "
                        "i.batch_id=:batch ORDER BY i.row_no"
                    ),
                    {"order": order["public_id"], "batch": batch_id},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            if row["work_item_id"] is None:
                original = await read_json(
                    session, "content_import_item", "candidate", {"public_id": row["public_id"]}
                )
                await session.execute(
                    text(
                        "INSERT INTO content_work_item(public_id,work_order_id,import_item_id,pos"
                        "ition,content_id,original_revision_id,source_snapshot,source_hash,histor"
                        "ical_issues) VALUES(:id,:order,:import,:pos,:content,:revision,:source,:"
                        "source_hash,:issues)"
                    ),
                    {
                        "id": new_ulid(datetime.now(UTC)),
                        "order": order["public_id"],
                        "import": row["public_id"],
                        "pos": row["row_no"],
                        "content": row["content_id"],
                        "revision": row["revision_id"],
                        "source": encode(original["source_snapshot"])
                        if original.get("source_snapshot")
                        else None,
                        "issues": encode(original.get("issues", [])),
                        "source_hash": snapshot_hash(original["source_snapshot"])
                        if original.get("source_snapshot")
                        else None,
                    },
                )
            elif (row["content_id"] and not row["work_content_id"]) or (
                row["revision_id"] and not row["work_original_revision"]
            ):
                await session.execute(
                    text(
                        "UPDATE content_work_item SET content_id=COALESCE(content_id,:content),or"
                        "iginal_revision_id=COALESCE(original_revision_id,:revision) WHERE "
                        "public_id=:id"
                    ),
                    {
                        "id": row["work_item_id"],
                        "content": row["content_id"],
                        "revision": row["revision_id"],
                    },
                )

    async def sync_materials(self, session: AsyncSession, order: dict[str, Any]) -> None:
        manifest = (
            await read_json(
                session, "content_import_batch", "manifest", {"public_id": order["import_batch_id"]}
            )
            or {}
        )
        bindings = manifest.get("binding_requests", {})
        if not bindings:
            raise AppError("WORK_MATERIAL_BIND_REQUIRED", "请先在素材入口选择并绑定实际草稿", 409)
        for position, (stage_id, binding) in enumerate(bindings.items(), 1):
            revision_id = binding.get("request", {}).get("revision_id")
            if not revision_id:
                continue
            sql = (
                (
                    "SELECT c.public_id AS content_id FROM short_reading_revision r JOIN "
                    "short_reading c ON c.public_id=r.short_reading_id WHERE "
                    "r.public_id=:revision AND c.series_id=:series"
                )
                if order["content_type"] == "short_reading"
                else (
                    "SELECT c.public_id AS content_id FROM scene_revision r JOIN scene c ON "
                    "c.id=r.scene_id JOIN content_series cs ON cs.id=c.series_id JOIN "
                    "content_template t ON t.id=c.template_id WHERE r.public_id=:revision AND "
                    "cs.public_id=:series AND t.template_type IN (:type,:legacy_type)"
                )
            )
            content_id = await session.scalar(
                text(sql),
                {
                    "revision": revision_id,
                    "series": order["series_id"],
                    "type": order["content_type"],
                    "legacy_type": "vocabulary"
                    if order["content_type"] == "word_card"
                    else order["content_type"],
                },
            )
            if not content_id:
                raise AppError("WORK_MATERIAL_TARGET_INVALID", "素材绑定没有可信的内容归属", 409)
            item_id = await session.scalar(
                text(
                    "SELECT public_id FROM content_work_item WHERE work_order_id=:order AND "
                    "content_id=:content"
                ),
                {"order": order["public_id"], "content": content_id},
            )
            if item_id is None:
                item_id = new_ulid(datetime.now(UTC))
                await session.execute(
                    text(
                        "INSERT INTO content_work_item(public_id,work_order_id,position,content_i"
                        "d,original_revision_id) VALUES(:id,:order,:pos,:content,:revision)"
                    ),
                    {
                        "id": item_id,
                        "order": order["public_id"],
                        "pos": position,
                        "content": content_id,
                        "revision": revision_id,
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO content_work_item_stage(work_item_id,stage_id,revision_id) "
                    "SELECT :item,public_id,:revision FROM content_import_stage WHERE "
                    "public_id=:stage AND item_id=:batch ON DUPLICATE KEY UPDATE "
                    "content_work_item_stage.id=content_work_item_stage.id"
                ),
                {
                    "item": item_id,
                    "revision": revision_id,
                    "stage": stage_id,
                    "batch": order["import_batch_id"],
                },
            )

    async def link_tasks(self, session: AsyncSession, order_id: str) -> None:
        # History is append-only, including reading drafts replaced by a new revision.
        # Batch confirmation has an exact persisted row scope, even before any revision exists.
        await session.execute(
            text(
                "INSERT IGNORE INTO content_work_item_stage(work_item_id,stage_id,revision_id) "
                "SELECT w.public_id,s.public_id,NULL FROM content_work_item w "
                "JOIN content_work_order b ON b.public_id=w.work_order_id "
                "JOIN content_import_item i ON i.public_id=w.import_item_id AND "
                "i.batch_id=b.import_batch_id "
                "JOIN admin_outbox o ON o.aggregate_public_id=b.import_batch_id "
                "JOIN content_import_stage s ON s.public_id=JSON_UNQUOTE(JSON_EXTRACT(o.payload,'"
                "$.stage_id')) "
                "AND s.item_id=b.import_batch_id WHERE w.work_order_id=:id AND w.cancelled=0 "
                "AND w.content_id IS NULL AND i.status NOT IN ('SUCCESS','CANCELLED') "
                "AND JSON_UNQUOTE(JSON_EXTRACT(o.payload,'$.batch_id'))=b.import_batch_id AND ("
                "(s.stage='VALIDATE' AND o.event_type='content.import.validate') OR "
                "(s.stage='CONFIRM' AND o.event_type='CONTENT_IMPORT_CONFIRM' AND "
                "JSON_CONTAINS(JSON_EXTRACT(o.payload,'$.confirmation.selected'),JSON_QUOTE(w.imp"
                "ort_item_id))))"
            ),
            {"id": order_id},
        )
        await session.execute(
            text(
                "INSERT IGNORE INTO content_work_item_revision(work_item_id,revision_id) SELECT "
                "w.public_id,r.public_id FROM content_work_item w JOIN scene s ON "
                "s.public_id=w.content_id JOIN scene_revision r ON r.id=s.draft_revision_id "
                "WHERE w.work_order_id=:id UNION SELECT w.public_id,s.draft_revision_id FROM "
                "content_work_item w JOIN short_reading s ON s.public_id=w.content_id WHERE "
                "w.work_order_id=:id AND s.draft_revision_id IS NOT NULL UNION SELECT "
                "public_id,original_revision_id FROM content_work_item WHERE work_order_id=:id "
                "AND original_revision_id IS NOT NULL"
            ),
            {"id": order_id},
        )
        await session.execute(
            text(
                "INSERT IGNORE INTO content_work_item_stage(work_item_id,stage_id,revision_id,sen"
                "tence_id) SELECT h.work_item_id,s.public_id,h.revision_id,NULL FROM "
                "content_work_item_revision h JOIN content_work_item w ON "
                "w.public_id=h.work_item_id JOIN content_import_stage s ON "
                "s.item_id=h.revision_id WHERE w.work_order_id=:id"
            ),
            {"id": order_id},
        )
        # Sentence stage.item_id is a hash. Outbox revision/sentence metadata supplies ownership.
        await session.execute(
            text(
                "INSERT IGNORE INTO content_work_item_stage(work_item_id,stage_id,revision_id,sen"
                "tence_id) SELECT h.work_item_id,s.public_id,h.revision_id,JSON_UNQUOTE(JSON_EXTR"
                "ACT(o.payload,'$.sentence_id')) FROM content_work_item_revision h JOIN "
                "content_work_item w ON w.public_id=h.work_item_id JOIN admin_outbox o ON "
                "o.aggregate_public_id=h.revision_id AND o.event_type='CONTENT_SENTENCE_SLICE' "
                "JOIN content_import_stage s ON s.public_id=JSON_UNQUOTE(JSON_EXTRACT(o.payload,'"
                "$.stage_id')) WHERE w.work_order_id=:id AND "
                "JSON_UNQUOTE(JSON_EXTRACT(o.payload,'$.revision_id'))=h.revision_id"
            ),
            {"id": order_id},
        )
        await session.execute(
            text(
                "INSERT IGNORE INTO content_work_item_stage(work_item_id,stage_id,revision_id,sen"
                "tence_id) SELECT h.work_item_id,s.public_id,h.revision_id,NULL FROM "
                "content_work_item_revision h JOIN content_work_item w ON "
                "w.public_id=h.work_item_id JOIN lexicon_candidate c ON "
                "c.revision_id=h.revision_id JOIN content_import_stage s ON "
                "s.public_id=JSON_UNQUOTE(JSON_EXTRACT(c.candidate,'$.stage_id')) OR "
                "s.public_id=JSON_UNQUOTE(JSON_EXTRACT(c.candidate,'$.poll_stage_id')) WHERE "
                "w.work_order_id=:id"
            ),
            {"id": order_id},
        )

    async def all(
        self,
        order: dict[str, Any],
        *,
        original_item_id: str | None = None,
        include_originals: bool = False,
    ) -> list[WorkItemDetail]:
        async with self.sessions() as session, session.begin():
            # Order lock serializes local registration, never crosses a delegate transaction.
            await session.execute(
                text("SELECT public_id FROM content_work_order WHERE public_id=:id FOR UPDATE"),
                {"id": order["public_id"]},
            )
            await self.sync_imports(session, order)
            await self.link_tasks(session, order["public_id"])
            publication_results: dict[str, Any] = {}
            publication_candidates: dict[str, Any] = {}
            from .anomalies import unresolved_within

            sample_failures = await unresolved_within(session, order["public_id"])
            if order["publication_token"]:
                range_state = (
                    await read_json(
                        session,
                        "publication_range",
                        "results",
                        {"public_id": order["publication_token"]},
                    )
                    or {}
                )
                publication_results = range_state.get("items", {})
                publication_candidates = {
                    c["revision_id"]: c
                    for c in await read_json(
                        session,
                        "publication_range",
                        "candidates",
                        {"public_id": order["publication_token"]},
                    )
                    or []
                }
                await session.execute(
                    text(
                        "INSERT IGNORE INTO content_work_item_stage(work_item_id,stage_id,revisio"
                        "n_id) "
                        "SELECT h.work_item_id,p.stage_id,h.revision_id FROM "
                        "content_work_item_revision h "
                        "JOIN content_work_item w ON w.public_id=h.work_item_id JOIN "
                        "publication_range p ON p.public_id=:token "
                        "WHERE w.work_order_id=:id AND p.stage_id IS NOT NULL AND "
                        "JSON_CONTAINS(p.candidates,JSON_OBJECT('revision_id',h.revision_id))"
                    ),
                    {"id": order["public_id"], "token": order["publication_token"]},
                )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT w.public_id,w.import_item_id,w.content_id,w.original_revision"
                            "_id,w.cancelled,w.source_hash,w.source_snapshot IS NOT NULL AS "
                            "source_available,CASE WHEN CHAR_LENGTH(CAST(w.historical_issues AS "
                            "CHAR))<=8192 THEN w.historical_issues ELSE NULL END AS "
                            "small_issues,CHAR_LENGTH(CAST(w.historical_issues AS CHAR)) AS "
                            "issues_size,i.status AS import_status,JSON_UNQUOTE(JSON_EXTRACT(i.re"
                            "sult,'$.error_code')) AS import_error FROM content_work_item w LEFT "
                            "JOIN content_import_item i ON i.public_id=w.import_item_id WHERE "
                            "w.work_order_id=:id ORDER BY w.position,w.id"
                        ),
                        {"id": order["public_id"]},
                    )
                )
                .mappings()
                .all()
            )
            headers = (
                (
                    await session.execute(
                        text(
                            "SELECT w.content_id,r.public_id AS revision_id,r.edit_version,r.edit"
                            "_version AS content_version,s.status AS lifecycle_status,r.status "
                            "AS revision_status,s.draft_revision_id IS NOT NULL AS current_draft,"
                            "p.public_id AS published_revision_id FROM "
                            "content_work_item w JOIN scene s ON s.public_id=w.content_id JOIN "
                            "scene_revision r ON r.id=COALESCE(s.draft_revision_id,s.published_re"
                            "vision_id) LEFT JOIN scene_revision p ON "
                            "p.id=s.published_revision_id WHERE w.work_order_id=:id UNION SELECT "
                            "w.content_id,r.public_id,r.edit_version,s.version,s.status,r.status,"
                            "s.draft_revision_id IS NOT NULL,"
                            "s.published_revision_id FROM content_work_item w JOIN short_reading "
                            "s ON s.public_id=w.content_id JOIN short_reading_revision r ON "
                            "r.public_id=COALESCE(s.draft_revision_id,s.published_revision_id) "
                            "WHERE w.work_order_id=:id"
                        ),
                        {"id": order["public_id"]},
                    )
                )
                .mappings()
                .all()
            )
            tasks = (
                (
                    await session.execute(
                        text(
                            "SELECT a.work_item_id,a.revision_id,a.sentence_id,s.public_id AS "
                            "stage_id,s.stage,s.status,s.input_hash,s.config_version,s.fencing_ve"
                            "rsion,s.attempt,s.error_code FROM content_work_item_stage a JOIN "
                            "content_work_item w ON w.public_id=a.work_item_id JOIN "
                            "content_import_stage s ON s.public_id=a.stage_id WHERE "
                            "w.work_order_id=:id ORDER BY s.id"
                        ),
                        {"id": order["public_id"]},
                    )
                )
                .mappings()
                .all()
            )
            originals = {}
            histories = {}
            raw_histories = {}
            for row in rows:
                identity = {"public_id": row["public_id"]}
                originals[row["public_id"]] = (
                    await read_json(session, "content_work_item", "source_snapshot", identity)
                    if row["source_available"]
                    and (include_originals or row["public_id"] == original_item_id)
                    else None
                )
                small = row["small_issues"]
                historical = (
                    await read_json(session, "content_work_item", "historical_issues", identity)
                    if row["issues_size"] and row["issues_size"] > 8192
                    else (json.loads(small) if isinstance(small, str) else small)
                )
                raw_histories[row["public_id"]] = historical or []
                histories[row["public_id"]] = issues(
                    historical or [],
                    historical=True,
                    source_missing=not row["content_id"],
                )
            resolutions = (
                (
                    await session.execute(
                        text(
                            "SELECT "
                            "r.work_item_id,r.issue_id,r.source_ha"
                            "sh,r.candidate_hash,r.dependency_hash "
                            "FROM content_work_issue_resolution r JOIN content_work_item w ON "
                            "w.public_id=r.work_item_id WHERE w.work_order_id=:id"
                        ),
                        {"id": order["public_id"]},
                    )
                )
                .mappings()
                .all()
            )

        by_content = {row["content_id"]: dict(row) for row in headers}
        gate = asyncio.Semaphore(8)

        async def facts(header: dict[str, Any]) -> dict[str, Any]:
            async with gate, self.sessions() as session, session.begin():
                table = (
                    "short_reading_revision"
                    if order["content_type"] == "short_reading"
                    else "scene_revision"
                )
                snapshot = await read_json(
                    session, table, "content_snapshot", {"public_id": header["revision_id"]}
                )
                digest = snapshot_hash(snapshot)
                f = await self.proofreading.observation_facts(
                    session, order["content_type"], header["revision_id"], snapshot
                )
                from .effective_issues import current_issues_within

                effective_source_issues = await current_issues_within(
                    session, header["content_id"], header["revision_id"], snapshot
                )
                stage_current: dict[str, bool] = {}
                slice_hashes: dict[str, str] = {}
                if order["content_type"] != "short_reading":
                    from .task_dependencies import stage_dependencies

                    stage_current, slice_hashes = await stage_dependencies(
                        session,
                        header["revision_id"],
                        SceneContent.model_validate(snapshot),
                        int(header["edit_version"]),
                    )
                approved = await self.proofreading.is_confirmed_within(
                    session, header["revision_id"], digest
                )
                confirmed = bool(
                    await session.scalar(
                        text(
                            "SELECT id FROM proofreading_confirmation WHERE revision_id=:id AND "
                            "candidate_hash=:hash LIMIT 1"
                        ),
                        {"id": header["revision_id"], "hash": digest},
                    )
                )
                return {
                    **header,
                    "snapshot": snapshot,
                    "candidate_hash": digest,
                    "facts": f,
                    "effective_issues": effective_source_issues,
                    "stage_current": stage_current,
                    "slice_hashes": slice_hashes,
                    "confirmed": confirmed,
                    "approved": approved,
                }

        current = dict(
            zip(
                by_content,
                await asyncio.gather(*(facts(h) for h in by_content.values())),
                strict=True,
            )
        )
        task_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task_row in tasks:
            task_map[task_row["work_item_id"]].append(dict(task_row))
        result = []
        for row in rows:
            identity = row["public_id"]
            candidate = current.get(row["content_id"])
            snapshot = candidate["snapshot"] if candidate else {}
            original = originals[identity]
            digest = candidate["candidate_hash"] if candidate else None
            effective = []
            evaluated = effective_history(
                raw_histories[identity],
                snapshot,
                row["source_hash"],
                [dict(r) for r in resolutions if r["work_item_id"] == identity],
                imported=bool(candidate),
            )
            histories[identity] = issues(evaluated, historical=True)
            for issue, value in zip(histories[identity], evaluated, strict=True):
                issue.resolved = value["resolved"]
                issue.origin_work_item_id = identity
                issue.source_hash = row["source_hash"]
                issue.acknowledgement_eligible = bool(
                    row["source_available"] and row["source_hash"] and not issue.resolved
                )
                if not issue.resolved:
                    effective.append(issue.model_copy(update={"historical": False}))
            f = candidate["facts"] if candidate else {}
            errors = list(f.get("error_codes", []))
            if candidate:
                known_ids = {issue.id for issue in effective}
                effective.extend(
                    issue
                    for issue in issues(candidate["effective_issues"])
                    if issue.id not in known_ids
                )
            effective.extend(issues([{"code": code} for code in sorted(set(errors))]))
            if (
                candidate
                and (order["content_type"], row["content_id"], candidate["revision_id"], digest)
                in sample_failures
            ):
                effective.extend(
                    issues(
                        [
                            {
                                "code": "MANUAL_REVIEW_REQUIRED",
                                "message": "抽样发现问题,请对照原文修正或在抽样中逐项重新检查",
                            }
                        ]
                    )
                )
            if candidate and not f.get("normal", f.get("ready", False)) and not effective:
                effective.extend(
                    issues(
                        [
                            {
                                "code": "CONTENT_PREPARATION_REQUIRED",
                                "message": "正文或素材尚未准备完成",
                            }
                        ]
                    )
                )
            item_tasks = []
            latest_stage = {
                (t["revision_id"], t["stage"], t["sentence_id"]): t["stage_id"]
                for t in task_map[identity]
            }
            latest_import_stage = {
                t["stage"]: t["stage_id"]
                for t in task_map[identity]
                if t["revision_id"] is None and t["stage"] in {"VALIDATE", "CONFIRM"}
            }
            for task in task_map[identity]:
                task.pop("work_item_id")
                valid = bool(candidate and task["revision_id"] == candidate["revision_id"])
                valid &= (
                    latest_stage.get((task["revision_id"], task["stage"], task["sentence_id"]))
                    == task["stage_id"]
                )
                if task["revision_id"] is None and task["stage"] in {"VALIDATE", "CONFIRM"}:
                    valid = bool(
                        not candidate
                        and not row["cancelled"]
                        and row["import_item_id"]
                        and row["import_status"] not in {"SUCCESS", "CANCELLED"}
                        and latest_import_stage.get(task["stage"]) == task["stage_id"]
                        and task["status"] != "CANCELLED"
                    )
                if valid and task["stage"] == "AUTO_AUDIO":
                    valid = task["input_hash"] == audio_input_hash(
                        SceneContent.model_validate(snapshot)
                    )
                if (
                    valid
                    and candidate
                    and task["stage"] in {"AI_LEXICON", "ASR_BOUNDARY", "ASR_POLL"}
                ):
                    valid = candidate["stage_current"].get(task["stage_id"], False)
                if valid and candidate and task["stage"] == "SENTENCE_SLICE":
                    valid = candidate["slice_hashes"].get(task["sentence_id"]) == task["input_hash"]
                item_tasks.append(
                    WorkTask(**task, current=valid, retryable=valid and task["status"] == "FAILED")
                )
            active = any(
                t.current and t.status in {"PENDING", "QUEUED", "RUNNING", "RETRY"}
                for t in item_tasks
            )
            failed = (row["import_status"] == "FAILED" and not active) or any(
                t.current and t.status == "FAILED" for t in item_tasks
            )
            if candidate:
                publication = publication_results.get(candidate["revision_id"], {})
                frozen_publication = publication_candidates.get(candidate["revision_id"], {})
                if (
                    publication.get("status") in {"FAILED", "UNKNOWN"}
                    and frozen_publication.get("candidate_hash") == digest
                ):
                    failed = True
                    effective.extend(
                        issues(
                            [
                                {
                                    "code": publication.get("error_code", "PUBLICATION_FAILED"),
                                    "message": "此篇发布未完成,请重试冻结范围中的失败项",
                                }
                            ]
                        )
                    )
                    effective[-1].correction_target = "publication"
            if row["import_error"]:
                effective.extend(
                    issues([{"code": row["import_error"]}], source_missing=not candidate)
                )
            published = bool(candidate and candidate["lifecycle_status"] == "PUBLISHED")
            unchanged_published = bool(
                candidate and candidate["published_revision_id"] == candidate["revision_id"]
            )
            status: ProductionStatus = (
                "cancelled"
                if row["cancelled"]
                else "published"
                if unchanged_published
                else "failed"
                if failed
                else (
                    "processing"
                    if active
                    else "normal"
                    if candidate and not effective and f.get("normal", f.get("ready", False))
                    else "review_required"
                )
            )
            confirmed = bool(candidate and candidate["confirmed"])
            model = None
            if candidate:
                model = (
                    ReadingCandidate(
                        template="short_reading", content=ReadingContent.model_validate(snapshot)
                    )
                    if order["content_type"] == "short_reading"
                    else SceneCandidate(
                        template=order["content_type"],
                        content=SceneContent.model_validate(snapshot),
                    )
                )
            result.append(
                WorkItemDetail(
                    item_id=identity,
                    work_order_id=order["public_id"],
                    import_item_id=row["import_item_id"],
                    content_id=row["content_id"],
                    original_revision_id=row["original_revision_id"],
                    revision_id=candidate["revision_id"] if candidate else None,
                    candidate_hash=digest,
                    edit_version=candidate["edit_version"] if candidate else None,
                    content_version=candidate["content_version"] if candidate else None,
                    title_en=snapshot.get("title_en", ""),
                    title_zh=snapshot.get("title_zh", ""),
                    lifecycle_status=candidate["lifecycle_status"] if candidate else "DRAFT",
                    production_status=status,
                    issues=effective,
                    tasks=item_tasks,
                    confirmed=confirmed,
                    publishable=bool(
                        candidate
                        and candidate["approved"]
                        and f.get("ready")
                        and not effective
                        and not active
                        and not failed
                        and not unchanged_published
                    ),
                    published=published,
                    current_draft=bool(candidate and candidate["current_draft"]),
                    current=model,
                    source_available=bool(row["source_available"]),
                    source_snapshot=original,
                    historical_issues=histories[identity],
                    phase_checks=f.get("phase_checks", {}),
                )
            )
        return result
