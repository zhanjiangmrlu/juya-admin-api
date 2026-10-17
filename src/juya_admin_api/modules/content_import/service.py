"""Durable import previews and per-item transactional identity mapping."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore, decode, encode
from juya_admin_api.modules.content.schemas import DialogueSentence, SceneContent, SceneEntry
from juya_admin_api.modules.content.v15_contracts import IMPORT_NAMESPACE
from juya_admin_api.modules.content_import.parser import (
    ImportLimits,
    SpoolFiles,
    canonical_hash,
    material_semantic_hash,
    parse_docx,
    parse_package,
    read_container,
    semantic_input,
)
from juya_admin_api.modules.content_import.stages import Claim, StageStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

BindMedia = Callable[[bytes, str, str, str], Awaitable[dict[str, Any]]]


class ImportService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        storage: Path,
        production: ProductionStore,
        readings: ShortReadingStore,
        bind_media: BindMedia,
        limits: ImportLimits | None = None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage.resolve()
        self.production = production
        self.readings = readings
        self.bind_media = bind_media
        self.limits = limits or ImportLimits()
        self.stages = StageStore(sessions)

    def path(self, asset_id: str) -> Path:
        if len(asset_id) != 32 or any(c not in "0123456789abcdef" for c in asset_id):
            raise AppError("IMPORT_ARTIFACT_INVALID", "导入原文件标识无效", 409)
        return self.storage / (asset_id + ".bin")

    async def create(
        self, *, series_id: str, content_type: str, mode: str, data: bytes, actor: str, key: str
    ) -> dict[str, Any]:
        if content_type not in {"dialogue", "word_card", "short_reading"} or mode not in {
            "ZIP",
            "DOCX",
        }:
            raise AppError("IMPORT_TYPE_INVALID", "导入类型无效", 422)
        if not data or len(data) > self.limits.max_package_bytes:
            raise AppError("IMPORT_PACKAGE_TOO_LARGE", "原文件为空或超出导入限制", 422)
        digest = hashlib.sha256(data).hexdigest()
        request_hash = canonical_hash([series_id, content_type, mode, digest])
        now = datetime.now(UTC)
        identity, asset = new_ulid(now), uuid4().hex
        self.storage.mkdir(parents=True, exist_ok=True)
        path = self.path(asset)
        await asyncio.to_thread(path.write_bytes, data)
        try:
            async with self.sessions() as session, session.begin():
                if not await session.scalar(
                    text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                    {"id": series_id},
                ):
                    raise AppError("SERIES_NOT_FOUND", "系列不存在", 404)
                replay = await self.production.creation_replay(
                    session, "v15.import.create", actor, key, request_hash
                )
                if replay:
                    path.unlink(missing_ok=True)
                    return replay
                await session.execute(
                    text(
                        "INSERT INTO "
                        "content_import_batch(public_id,namespace,series_id,content_type,"
                        "import_mode,asset_id,input_hash,status,created_by) "
                        "VALUES(:id,:namespace,:series,:type,:mode,:asset,:hash,'PENDING',:actor)"
                    ),
                    {
                        "id": identity,
                        "namespace": IMPORT_NAMESPACE,
                        "series": series_id,
                        "type": content_type,
                        "mode": mode,
                        "asset": asset,
                        "hash": digest,
                        "actor": actor,
                    },
                )
                stage = await self.stages.ensure_within(
                    session, identity, "VALIDATE", digest, "v15.1"
                )
                await session.execute(
                    text(
                        "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,payload) "
                        "VALUES(:event,'content.import.validate',:id,:payload)"
                    ),
                    {
                        "event": new_ulid(now),
                        "id": identity,
                        "payload": encode({"batch_id": identity, "stage_id": stage}),
                    },
                )
                result = {"batch_id": identity, "status": "PENDING", "input_hash": digest}
                await self.production.remember_creation(
                    session, "v15.import.create", actor, key, request_hash, result, now
                )
                return result
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def batch(
        self,
        session: AsyncSession,
        identity: str,
        lock: bool = False,
        *,
        preview_hash_only: bool = False,
    ) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id,public_id,namespace,series_id,content_type,import_mode,asset_id,"
                        "input_hash,preview_version,status,created_by,created_at,cancelled_at "
                        "FROM content_import_batch WHERE public_id=:id"
                        + (" FOR UPDATE" if lock else " FOR SHARE")
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise AppError("IMPORT_BATCH_NOT_FOUND", "导入批次不存在", 404)
        result = dict(row)
        if preview_hash_only:
            frozen_hash = await session.scalar(
                text(
                    "SELECT JSON_UNQUOTE(JSON_EXTRACT(manifest,'$.preview.hash')) "
                    "FROM content_import_batch WHERE public_id=:id FOR SHARE"
                ),
                {"id": identity},
            )
            result["manifest"] = {"preview": {"hash": frozen_hash}}
        else:
            result["manifest"] = await read_json(
                session, "content_import_batch", "manifest", {"public_id": identity}
            )
        return result

    async def original(self, batch: dict[str, Any]) -> bytes:
        try:
            data = await asyncio.to_thread(self.path(batch["asset_id"]).read_bytes)
        except OSError as error:
            raise AppError(
                "IMPORT_ARTIFACT_UNAVAILABLE", "原文件不可读取,请重新上传", 409
            ) from error
        if (
            len(data) > self.limits.max_package_bytes
            or hashlib.sha256(data).hexdigest() != batch["input_hash"]
        ):
            raise AppError("IMPORT_ARTIFACT_CHANGED", "原文件已变化或超限", 409)
        return data

    async def validate(self, batch_id: str, stage_id: str) -> None:
        now = datetime.now(UTC)
        claim = await self.stages.claim(stage_id, uuid4().hex, now)
        if claim is None:
            return
        try:
            async with self.sessions() as session:
                batch = await self.batch(session, batch_id)
            if batch["status"] == "CANCELLED":
                return
            data = await self.original(batch)
            if batch["import_mode"] == "ZIP":
                manifest = await asyncio.to_thread(
                    parse_package, data, batch["content_type"], limits=self.limits
                )
            else:
                articles = await asyncio.to_thread(
                    parse_docx, data, batch["content_type"], self.limits
                )
                if len(articles) > self.limits.max_items:
                    raise AppError("IMPORT_ITEM_LIMIT", "内容数量超限", 422)
                manifest = {
                    "namespace": IMPORT_NAMESPACE,
                    "content_type": batch["content_type"],
                    "items": [
                        {
                            "row_no": i + 1,
                            "import_content_id": canonical_hash([batch["input_hash"], i])[:64],
                            "candidate": article,
                            "source_snapshot": deepcopy(article["source_snapshot"]),
                            "entries": deepcopy(article["parsed_entries"]),
                            "resources": {},
                            "issues": deepcopy(article["parsing_issues"]),
                            "defaults": ["default_cover"],
                            "input_hash": canonical_hash(
                                semantic_input(article)
                                | (
                                    {"imported_entries": semantic_input(article["parsed_entries"])}
                                    if article["parsed_entries"]
                                    else {}
                                )
                            ),
                            "expected_content_version": None,
                        }
                        for i, article in enumerate(articles)
                    ],
                    "orphan_entry_rows": [],
                    "unreferenced_files": [],
                }
            if not manifest["items"]:
                raise AppError("IMPORT_EMPTY", "未解析到内容", 422)
            async with self.sessions() as session, session.begin():
                current = await self.batch(session, batch_id, True)
                await self.assert_claim_within(session, claim, batch_id)
                if current["status"] == "CANCELLED" or current["input_hash"] != batch["input_hash"]:
                    raise AppError("IMPORT_CANCELLED", "批次已取消或发生变化", 409)
                # A stale validator cannot overwrite a newer preview.
                if current["status"] == "PENDING":
                    for item in manifest["items"]:
                        await session.execute(
                            text(
                                "INSERT INTO "
                                "content_import_item(public_id,batch_id,row_no,import_content_id,"
                                "input_hash,expected_version,operation,status,candidate,issues) "
                                "VALUES(:id,:batch,:row,:external,:hash,:expected,'PREVIEW',"
                                "'PENDING',:candidate,:issues)"
                            ),
                            {
                                "id": new_ulid(datetime.now(UTC)),
                                "batch": batch_id,
                                "row": item["row_no"],
                                "external": item["import_content_id"],
                                "hash": item["input_hash"],
                                "expected": item["expected_content_version"],
                                "candidate": encode(item),
                                "issues": encode(item["issues"]),
                            },
                        )
                    await session.execute(
                        text(
                            "UPDATE content_import_batch SET "
                            "manifest=:manifest,status='PREVIEW_READY',"
                            "preview_version=1 WHERE public_id=:id"
                        ),
                        {"id": batch_id, "manifest": encode(manifest)},
                    )
                await self.assert_claim_within(session, claim, batch_id)
                await session.execute(
                    text(
                        "UPDATE content_import_stage SET status='SUCCEEDED',output=:output,"
                        "lease_owner=NULL,lease_until=NULL WHERE public_id=:id"
                    ),
                    {"id": claim.id, "output": encode({"batch_id": batch_id})},
                )
        except AppError as error:
            await self.stages.fail(claim, error.code, datetime.now(UTC), retry=False)
            async with self.sessions() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE content_import_batch SET status='FAILED',manifest=:manifest "
                        "WHERE public_id=:id AND status='PENDING'"
                    ),
                    {"id": batch_id, "manifest": encode({"error_code": error.code})},
                )

    async def current_mapping(
        self, session: AsyncSession, kind: str, external: str, lock: bool = False
    ) -> dict[str, Any] | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT namespace,content_type,import_content_id,content_id,"
                        "input_hash,version "
                        "FROM import_content_mapping WHERE namespace=:namespace AND "
                        "content_type=:type "
                        "AND import_content_id=:external" + (" FOR UPDATE" if lock else "")
                    ),
                    {"namespace": IMPORT_NAMESPACE, "type": kind, "external": external},
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        result = dict(row)
        if kind == "short_reading":
            content = (
                (
                    await session.execute(
                        text(
                            "SELECT series_id,version AS current_version,status,draft_revision_id,"
                            "published_revision_id "
                            "FROM short_reading WHERE public_id=:id"
                            + (" FOR UPDATE" if lock else "")
                        ),
                        {"id": row["content_id"]},
                    )
                )
                .mappings()
                .first()
            )
        else:
            content = (
                (
                    await session.execute(
                        text(
                            "SELECT cs.public_id AS series_id,"
                            "COALESCE(r.edit_version,p.edit_version,0) AS "
                            "current_version,"
                            "s.status,r.public_id AS draft_revision_id,p.public_id AS "
                            "published_revision_id FROM scene s JOIN "
                            "content_series cs "
                            "ON cs.id=s.series_id LEFT JOIN scene_revision r ON "
                            "r.id=s.draft_revision_id LEFT JOIN scene_revision p ON "
                            "p.id=s.published_revision_id "
                            "WHERE s.public_id=:id" + (" FOR UPDATE" if lock else "")
                        ),
                        {"id": row["content_id"]},
                    )
                )
                .mappings()
                .first()
            )
        if not content:
            raise AppError("IMPORT_MAPPING_BROKEN", "映射内容缺失,需人工修复", 409)
        result.update(dict(content))
        # MySQL may return Decimal for COALESCE; the frozen preview is JSON data.
        result["current_version"] = int(result["current_version"])
        return result

    async def semantic_replay(
        self,
        session: AsyncSession,
        batch: dict[str, Any],
        item: dict[str, Any],
        mapping: dict[str, Any],
    ) -> bool:
        """Require the mapped successful import's exact scope and material evidence."""
        if batch["import_mode"] != "ZIP" or not item.get("semantic_hash"):
            return False
        previous_id = await session.scalar(
            text(
                "SELECT i.public_id FROM content_import_item i JOIN content_import_batch b "
                "ON b.public_id=i.batch_id WHERE b.namespace=:namespace AND "
                "b.content_type=:type AND i.import_content_id=:external AND "
                "i.content_id=:content AND i.input_hash=:hash AND i.status='SUCCESS' AND "
                "JSON_UNQUOTE(JSON_EXTRACT(i.result,'$.content_id'))=:content "
                "AND JSON_UNQUOTE(JSON_EXTRACT(i.result,'$.revision_id'))=i.revision_id "
                "ORDER BY i.id DESC LIMIT 1"
            ),
            dict(
                namespace=IMPORT_NAMESPACE,
                type=batch["content_type"],
                external=item["import_content_id"],
                content=mapping["content_id"],
                hash=mapping["input_hash"],
            ),
        )
        if not previous_id:
            return False
        previous = await read_json(
            session, "content_import_item", "candidate", {"public_id": previous_id}
        )
        if (
            not isinstance(previous, dict)
            or not isinstance(previous.get("candidate"), dict)
            or not isinstance(previous.get("entries"), list)
            or not isinstance(previous.get("resources"), dict)
        ):
            return False
        # Indexed scope/hash proves the row, not the retained JSON's completeness.
        # Reconstruct the supported original material scheme before dropping DOCX evidence.
        retained_material = canonical_hash(
            {
                "rules": "v15.1",
                "type": batch["content_type"],
                "candidate": semantic_input(previous["candidate"]),
                "resources": previous["resources"],
                "entries": semantic_input(previous["entries"]),
            }
        )
        if retained_material != mapping["input_hash"]:
            return False
        return bool(
            material_semantic_hash(previous, batch["content_type"]) == item["semantic_hash"]
        )

    async def preview(self, batch_id: str) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            batch = await self.batch(session, batch_id, True)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,candidate,result,status FROM content_import_item "
                            "WHERE batch_id=:id ORDER BY row_no"
                        ),
                        {"id": batch_id},
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows:
                item = decode(row["candidate"])
                mapping = await self.current_mapping(
                    session, batch["content_type"], item["import_content_id"]
                )
                operation = (
                    "CREATE"
                    if mapping is None
                    else "SKIP"
                    if mapping["input_hash"] == item["input_hash"]
                    or await self.semantic_replay(session, batch, item, mapping)
                    else "UPDATE_DRAFT"
                )
                if mapping and mapping["series_id"] != batch["series_id"]:
                    item["issues"] = [
                        *item["issues"],
                        {"code": "IMPORT_SERIES_CONFLICT", "severity": "ERROR"},
                    ]
                if (
                    mapping
                    and operation == "UPDATE_DRAFT"
                    and item["expected_content_version"] is None
                ):
                    item["issues"] = [
                        *item["issues"],
                        {"code": "IMPORT_EXPECTED_VERSION_REQUIRED", "severity": "ERROR"},
                    ]
                if mapping and operation == "UPDATE_DRAFT":
                    for entry in item["entries"]:
                        prior_entry = (
                            await session.execute(
                                text(
                                    "SELECT m.entry_type,m.source_fingerprint,r.details "
                                    "FROM import_entry_mapping m JOIN content_entry_revision r "
                                    "ON r.entry_id=m.entry_id AND r.revision_id=:revision "
                                    "WHERE m.namespace=:ns AND m.content_type=:type "
                                    "AND m.content_id=:content AND m.import_entry_id=:external"
                                ),
                                {
                                    "revision": mapping["draft_revision_id"],
                                    "ns": IMPORT_NAMESPACE,
                                    "type": batch["content_type"],
                                    "content": mapping["content_id"],
                                    "external": entry["import_entry_id"],
                                },
                            )
                        ).first()
                        if prior_entry is None:
                            continue
                        from juya_admin_api.modules.content.v15_entries import source_identity

                        prior_details = decode(prior_entry.details)
                        if (
                            prior_entry.entry_type != entry["entry_type"]
                            or source_identity(SceneEntry.model_validate(prior_details))
                            != source_identity(
                                SceneEntry(
                                    source_sentence_ids=entry.get("source_sentence_ids", []),
                                    source_locator=entry.get("source_locator"),
                                    source_sentence=entry.get("source_sentence", ""),
                                )
                            )
                            or entry.get("meaning_change") == "new_sense"
                            or (
                                prior_details.get("chinese")
                                != (entry.get("meaning_zh") or entry.get("chinese", ""))
                                and entry.get("meaning_change") != "wording"
                            )
                        ):
                            item["issues"].append(
                                {
                                    "code": "IMPORT_ENTRY_IDENTITY_CHANGE_REQUIRED",
                                    "severity": "ERROR",
                                    "field": "import_entry_id",
                                    "import_entry_id": entry["import_entry_id"],
                                    "message": "来源或义项改变须使用新键,措辞修改须明确标记wording",
                                }
                            )
                item.update(
                    {
                        "item_id": row["public_id"],
                        "operation": operation,
                        "mapping": mapping,
                        "status": row["status"],
                        "result": decode(row["result"]),
                    }
                )
                items.append(item)
            result = {
                "batch_id": batch_id,
                "status": batch["status"],
                "preview_version": batch["preview_version"],
                "items": items,
                "manifest": decode(batch["manifest"]),
                "batch_issues": decode(batch["manifest"]).get("issues", []),
            }
            result["preview_hash"] = canonical_hash(
                {
                    "items": [
                        {
                            k: i[k]
                            for k in (
                                "item_id",
                                "input_hash",
                                "operation",
                                "mapping",
                                "issues",
                                "defaults",
                            )
                        }
                        for i in items
                    ],
                    "input": batch["input_hash"],
                    "version": batch["preview_version"],
                }
            )
            # Confirmation rechecks the exact mapping and versions shown here.
            manifest = decode(batch["manifest"])
            manifest["preview"] = {"hash": result["preview_hash"], "items": items}
            if batch["status"] != "CANCELLED":
                await session.execute(
                    text("UPDATE content_import_batch SET manifest=:manifest WHERE public_id=:id"),
                    {"id": batch_id, "manifest": encode(manifest)},
                )
            return result

    async def prepare(
        self, batch: dict[str, Any], item: dict[str, Any], files: Mapping[str, bytes] | None = None
    ) -> dict[str, Any]:
        result = dict(item)
        resources = item["resources"]
        if files is None:
            files = (
                await asyncio.to_thread(read_container, await self.original(batch), self.limits)
                if batch["import_mode"] == "ZIP"
                else {}
            )
        bound = {}
        for role, resource in resources.items():
            if role == "document_path":
                continue
            data = files[resource["path"]]
            if hashlib.sha256(data).hexdigest() != resource["sha256"]:
                raise AppError("IMPORT_ARTIFACT_CHANGED", "资源字节已变化", 409)
            bound[role] = await self.bind_media(
                data,
                "audio" if role == "audio_path" else "images",
                batch["created_by"],
                ("FULL_AUDIO" if batch["content_type"] == "short_reading" else "WHOLE_AUDIO")
                if role == "audio_path"
                else "COVER_IMAGE"
                if role == "cover_path"
                else "LEARNING_IMAGE",
            )
        result["bound"] = bound
        entries = []
        for entry in item["entries"]:
            prepared = dict(entry)
            for role, kind in (("image_path", "images"), ("pronunciation_path", "audio")):
                if entry.get(role):
                    if role == "image_path" and entry.get("entry_type") != "word":
                        raise AppError("CHUNK_IMAGE_INVALID", "语块不接收图片", 409)
                    data = files[entry[role]]
                    if hashlib.sha256(data).hexdigest() != entry[role + "_hash"]:
                        raise AppError("IMPORT_ARTIFACT_CHANGED", "条目资源已变化", 409)
                    prepared[role + "_bound"] = await self.bind_media(
                        data,
                        kind,
                        batch["created_by"],
                        ("WORD_AUDIO" if entry["entry_type"] == "word" else "CHUNK_AUDIO")
                        if role == "pronunciation_path"
                        else "WORD_IMAGE",
                    )
            entries.append(prepared)
        result["entries"] = entries
        return result

    async def confirm(
        self,
        batch_id: str,
        preview_hash: str,
        preview_version: int,
        selected: list[str],
        acknowledged_defaults: list[str],
        confirmed_conflicts: list[str],
        actor: str,
        key: str,
        claim: Claim | None = None,
    ) -> dict[str, Any]:
        if not selected or len(selected) != len(set(selected)):
            raise AppError("IMPORT_SELECTION_INVALID", "请选择唯一内容行", 422)
        request_hash = canonical_hash(
            [
                batch_id,
                preview_hash,
                preview_version,
                selected,
                acknowledged_defaults,
                confirmed_conflicts,
            ]
        )
        async with self.sessions() as session:
            replay = await self.production.creation_replay(
                session, "v15.import.confirm", actor, key, request_hash
            )
            if replay:
                return replay
            batch = await self.batch(session, batch_id)
            if claim:
                await self.assert_claim_within(session, claim, batch_id)
            preview = decode(batch["manifest"]).get("preview", {})
        if (
            batch["status"] not in {"PREVIEW_READY", "PARTIAL", "CONFIRMED"}
            or preview.get("hash") != preview_hash
            or batch["preview_version"] != preview_version
        ):
            raise AppError("IMPORT_PREVIEW_CHANGED", "预览已变化,请重新检查", 409)
        frozen = {i["item_id"]: i for i in preview["items"]}
        if set(selected) - frozen.keys():
            raise AppError("IMPORT_SELECTION_INVALID", "所选行不属于预览", 422)
        manifest = decode(batch["manifest"])
        if manifest.get("orphan_entry_rows") or any(
            issue.get("severity") == "ERROR" for issue in manifest.get("issues", [])
        ):
            raise AppError("IMPORT_BATCH_BLOCKED", "批次包含孤立词条或不适用附件,请修正原文件", 409)
        files: Mapping[str, bytes] = {}
        if any(frozen[item_id]["operation"] != "SKIP" for item_id in selected):
            data = await self.original(batch)
            if batch["import_mode"] == "ZIP":
                files = await asyncio.to_thread(read_container, data, self.limits, spool=True)
        try:
            results = []
            for item_id in selected:
                item = frozen[item_id]
                try:
                    # Already committed items resume without uploading their media again.
                    async with self.sessions() as session, session.begin():
                        current_batch = await self.batch(
                            session, batch_id, True, preview_hash_only=True
                        )
                        if claim:
                            await self.assert_claim_within(session, claim, batch_id)
                        if (
                            current_batch["status"] == "CANCELLED"
                            or decode(current_batch["manifest"]).get("preview", {}).get("hash")
                            != preview_hash
                        ):
                            raise AppError("IMPORT_PREVIEW_CHANGED", "批次预览已变化或取消", 409)
                        committed = (
                            await session.execute(
                                text(
                                    "SELECT status,result FROM content_import_item WHERE "
                                    "public_id=:id FOR UPDATE"
                                ),
                                {"id": item_id},
                            )
                        ).first()
                        if committed and committed.status == "SUCCESS":
                            results.append(decode(committed.result))
                            continue
                        if committed and committed.status == "CANCELLED":
                            raise AppError("IMPORT_ITEM_CANCELLED", "该导入行已取消", 409)
                    if any(
                        i["code"] == "IMPORT_ENTRY_IDENTITY_CHANGE_REQUIRED" for i in item["issues"]
                    ):
                        raise AppError(
                            "IMPORT_ENTRY_IDENTITY_CHANGE_REQUIRED",
                            "来源或义项改变须使用新导入键",
                            409,
                        )
                    if any(i["severity"] == "ERROR" for i in item["issues"]):
                        raise AppError("IMPORT_ITEM_BLOCKED", "该行存在阻断错误", 409)
                    if set(item["defaults"]) - set(acknowledged_defaults):
                        raise AppError(
                            "IMPORT_DEFAULT_CONFIRMATION_REQUIRED",
                            "请确认默认封面及无图片处理",
                            409,
                        )
                    if any(
                        i["severity"] == "CONFIRM"
                        and f"{item_id}:{i['field']}" not in confirmed_conflicts
                        for i in item["issues"]
                    ):
                        raise AppError(
                            "IMPORT_TEXT_CONFIRMATION_REQUIRED", "标题等冲突尚未确认", 409
                        )
                    prepared = (
                        await self.prepare(batch, item, files)
                        if item["operation"] != "SKIP"
                        else item
                    )
                    async with self.sessions() as session, session.begin():
                        current_batch = await self.batch(
                            session, batch_id, True, preview_hash_only=True
                        )
                        if claim:
                            await self.assert_claim_within(session, claim, batch_id)
                        if (
                            current_batch["status"] == "CANCELLED"
                            or decode(current_batch["manifest"]).get("preview", {}).get("hash")
                            != preview_hash
                        ):
                            raise AppError("IMPORT_PREVIEW_CHANGED", "批次预览已变化或取消", 409)
                        row = (
                            await session.execute(
                                text(
                                    "SELECT status,result FROM content_import_item WHERE "
                                    "public_id=:id FOR UPDATE"
                                ),
                                {"id": item_id},
                            )
                        ).first()
                        if row and row.status == "SUCCESS":
                            results.append(decode(row.result))
                            continue
                        if row and row.status == "CANCELLED":
                            raise AppError("IMPORT_ITEM_CANCELLED", "该导入行已取消", 409)
                        mapping = await self.current_mapping(
                            session, batch["content_type"], item["import_content_id"], True
                        )
                        if mapping != item["mapping"]:
                            raise AppError("IMPORT_TARGET_CHANGED", "目标草稿或映射已更新", 409)
                        if (
                            mapping
                            and item["operation"] == "UPDATE_DRAFT"
                            and int(item["expected_content_version"]) != mapping["current_version"]
                        ):
                            raise AppError(
                                "IMPORT_VERSION_CONFLICT", "目标版本与导入预期不一致", 409
                            )
                        result = await self.save_item_within(
                            session, batch, prepared, mapping, actor
                        )
                        result.update(
                            {
                                "item_id": item_id,
                                "status": "SUCCESS",
                                "operation": item["operation"],
                            }
                        )
                        await session.execute(
                            text(
                                "UPDATE content_import_item SET "
                                "status='SUCCESS',operation=:operation,content_id=:content,"
                                "revision_id=:revision,result=:result WHERE public_id=:id"
                            ),
                            {
                                "id": item_id,
                                "operation": item["operation"],
                                "content": result["content_id"],
                                "revision": result.get("revision_id"),
                                "result": encode(result),
                            },
                        )
                        await session.execute(
                            text(
                                "INSERT INTO "
                                "audit_event(public_id,actor_public_id,action,object_type,"
                                "object_public_id,"
                                "after_summary,request_id) "
                                "VALUES(:id,:actor,'content.import.confirm','import_item',:item,"
                                ":summary,:request)"
                            ),
                            {
                                "id": new_ulid(datetime.now(UTC)),
                                "actor": actor,
                                "item": item_id,
                                "summary": encode(result),
                                "request": key,
                            },
                        )
                        if claim:
                            await self.assert_claim_within(session, claim, batch_id)
                        results.append(result)
                except (AppError, ValueError, IntegrityError) as error:
                    if isinstance(error, AppError) and error.code in {
                        "IMPORT_STAGE_LEASE_LOST",
                        "IMPORT_PREVIEW_CHANGED",
                        "IMPORT_CANCELLED",
                    }:
                        raise
                    code = (
                        error.code
                        if isinstance(error, AppError)
                        else "IMPORT_CONCURRENT_CONFLICT"
                        if isinstance(error, IntegrityError)
                        else "IMPORT_CONTENT_INVALID"
                    )
                    results.append({"item_id": item_id, "status": "FAILED", "error_code": code})
                    async with self.sessions() as session, session.begin():
                        current_batch = await self.batch(
                            session, batch_id, True, preview_hash_only=True
                        )
                        if claim:
                            await self.assert_claim_within(session, claim, batch_id)
                        if current_batch["status"] == "CANCELLED":
                            raise AppError("IMPORT_CANCELLED", "批次已经取消", 409) from error
                        await session.execute(
                            text(
                                "UPDATE content_import_item SET status='FAILED',result=:result "
                                "WHERE public_id=:id AND status NOT IN ('SUCCESS','CANCELLED')"
                            ),
                            {"id": item_id, "result": encode(results[-1])},
                        )
            result = {
                "batch_id": batch_id,
                "items": results,
                "status": "PARTIAL"
                if any(i["status"] == "FAILED" for i in results)
                else "CONFIRMED",
            }
            async with self.sessions() as session, session.begin():
                final_batch = await self.batch(session, batch_id, True, preview_hash_only=True)
                if claim:
                    await self.assert_claim_within(session, claim, batch_id)
                if final_batch["status"] == "CANCELLED":
                    raise AppError("IMPORT_CANCELLED", "批次已经取消", 409)
                replay = await self.production.creation_replay(
                    session, "v15.import.confirm", actor, key, request_hash
                )
                if replay:
                    return replay
                await session.execute(
                    text(
                        "UPDATE content_import_batch SET status=:status WHERE public_id=:id AND "
                        "status<>'CANCELLED'"
                    ),
                    {"id": batch_id, "status": result["status"]},
                )
                persisted: list[Any] = list(
                    (
                        await session.execute(
                            text(
                                "SELECT result FROM content_import_item WHERE batch_id=:id "
                                "AND status IN ('SUCCESS','FAILED') ORDER BY row_no"
                            ),
                            {"id": batch_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                current_ids = {item["item_id"] for item in results}
                result["items"] = [
                    *results,
                    *[
                        decode(value)
                        for value in persisted
                        if value and decode(value)["item_id"] not in current_ids
                    ],
                ]
                result["status"] = (
                    "PARTIAL"
                    if any(item["status"] == "FAILED" for item in result["items"])
                    else "CONFIRMED"
                )
                await session.execute(
                    text(
                        "UPDATE content_import_batch SET status=:status WHERE public_id=:id "
                        "AND status<>'CANCELLED'"
                    ),
                    {"id": batch_id, "status": result["status"]},
                )
                await self.production.remember_creation(
                    session,
                    "v15.import.confirm",
                    actor,
                    key,
                    request_hash,
                    result,
                    datetime.now(UTC),
                )
            return result
        finally:
            if isinstance(files, SpoolFiles):
                await asyncio.to_thread(files.close)

    async def save_item_within(
        self,
        session: AsyncSession,
        batch: dict[str, Any],
        item: dict[str, Any],
        mapping: dict[str, Any] | None,
        actor: str,
    ) -> dict[str, Any]:
        if item["operation"] == "SKIP" and mapping:
            return {
                "content_id": mapping["content_id"],
                "revision_id": mapping["draft_revision_id"],
            }
        kind = batch["content_type"]
        identity = mapping["content_id"] if mapping else new_ulid(datetime.now(UTC))
        bound = item.get("bound", {})
        candidate = dict(item["candidate"])
        if kind == "short_reading":
            candidate = {
                k: v
                for k, v in candidate.items()
                if k
                in {
                    "title_en",
                    "title_zh",
                    "category",
                    "topic",
                    "level",
                    "safe_intro",
                    "paragraphs_en",
                    "translation_zh",
                }
            }
            candidate.update(
                {
                    "copyright_status": "",
                    "copyright_source": item["candidate"].get("copyright", ""),
                    "cover_asset_id": bound.get("cover_path", {}).get("asset_id"),
                    "audio": bound.get("audio_path"),
                }
            )
            if mapping:
                row = await self.readings.row(session, identity, True)
                revision = new_ulid(datetime.now(UTC))
                number = int(
                    await session.scalar(
                        text(
                            "SELECT COALESCE(MAX(version_no),0)+1 FROM short_reading_revision "
                            "WHERE short_reading_id=:id"
                        ),
                        {"id": identity},
                    )
                )
                await self.readings.insert_revision(
                    session,
                    identity,
                    revision,
                    self.readings.normalize(candidate),
                    actor,
                    number,
                    row["draft_revision_id"],
                )
                await session.execute(
                    text(
                        "UPDATE short_reading SET "
                        "draft_revision_id=:revision,version=version+1,updated_at=UTC_TIMESTAMP(6) "
                        "WHERE public_id=:id"
                    ),
                    {"id": identity, "revision": revision},
                )
                result = {"content_id": identity, "revision_id": revision}
            else:
                result = await self.readings.create_within(
                    session, batch["series_id"], candidate, actor, identity
                )
        else:
            candidate = {k: v for k, v in candidate.items() if k in SceneContent.model_fields}
            candidate["dialogue"] = [
                {k: v for k, v in sentence.items() if k in DialogueSentence.model_fields}
                for sentence in candidate.get("dialogue", [])
            ]
            candidate.update(
                {
                    "schema_version": 2,
                    "template_type": kind,
                    "cover_asset_id": bound.get("cover_path", {}).get("asset_id"),
                    "original_image_asset_id": bound.get("learning_image_path", {}).get("asset_id"),
                    "audio": bound.get("audio_path"),
                    "vocabulary": [],
                    "chunks": [],
                }
            )
            if not mapping:
                series = await session.scalar(
                    text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                    {"id": batch["series_id"]},
                )
                template = await self.production.template(
                    session, "vocabulary" if kind == "word_card" else "dialogue"
                )
                await session.execute(
                    text(
                        "INSERT INTO scene(public_id,series_id,template_id,title,status) "
                        "VALUES(:id,:series,:template,'','DRAFT')"
                    ),
                    {"id": identity, "series": series, "template": template},
                )
                await session.execute(
                    text(
                        "INSERT INTO content_identity(content_type,content_id,legacy_scene_id) "
                        "SELECT :type,:id,public_id FROM scene WHERE public_id=:id"
                    ),
                    {"id": identity, "type": kind},
                )
            existing_entry_mappings = {}
            for raw in item["entries"]:
                imported = (
                    (
                        await session.execute(
                            text(
                                "SELECT "
                                "entry_id,sense_id,source_id,entry_type,source_fingerprint "
                                "FROM import_entry_mapping "
                                "WHERE namespace=:ns AND content_type=:type AND "
                                "content_id=:content AND import_entry_id=:external FOR UPDATE"
                            ),
                            {
                                "ns": IMPORT_NAMESPACE,
                                "type": kind,
                                "content": identity,
                                "external": raw["import_entry_id"],
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if imported and imported["entry_type"] != raw["entry_type"]:
                    raise AppError("IMPORT_ENTRY_IDENTITY_CONFLICT", "导入条目类型不能改变", 409)
                if imported:
                    existing_entry_mappings[raw["import_entry_id"]] = imported
                entry = SceneEntry(
                    english=raw.get("english", ""),
                    chinese=raw.get("meaning_zh") or raw.get("chinese", ""),
                    explanation=raw.get("explanation_zh") or raw.get("explanation", ""),
                    phonetic=raw.get("phonetic", ""),
                    source_sentence=raw.get("source_sentence", ""),
                    source_sentence_ids=raw.get("source_sentence_ids", []),
                    entry_id=imported["entry_id"] if imported else "",
                    source_locator=raw.get("source_locator")
                    if isinstance(raw.get("source_locator"), dict)
                    else None,
                    icon_asset_id=raw.get("image_path_bound", {}).get("asset_id"),
                    audio_target_id=raw.get("pronunciation_path_bound", {}).get("target_id"),
                    audio_version_id=raw.get("pronunciation_path_bound", {}).get("version_id"),
                    pronunciation_text=raw.get("pronunciation_text", ""),
                    reading_variant=raw.get("reading_variant", ""),
                    meaning_change=raw.get("meaning_change") or None,
                )
                candidate["vocabulary" if raw["entry_type"] == "word" else "chunks"].append(
                    entry.model_dump()
                )
            scene_revision = SceneRevision(
                new_ulid(datetime.now(UTC)),
                identity,
                mapping["draft_revision_id"] if mapping else None,
                content=candidate,
                created_by=actor,
                created_at=datetime.now(UTC),
            )
            import_keys = {}
            positions = {"word": 0, "chunk": 0}
            for raw in item["entries"]:
                import_keys[(raw["entry_type"], positions[raw["entry_type"]])] = raw[
                    "import_entry_id"
                ]
                positions[raw["entry_type"]] += 1
            saved = await self.production.save_revision_within(
                session, scene_revision, None, import_keys=import_keys
            )
            positions = {"word": 0, "chunk": 0}
            for raw in item["entries"]:
                group = "vocabulary" if raw["entry_type"] == "word" else "chunks"
                entry = SceneContent.model_validate(saved.content).model_dump()[group][
                    positions[raw["entry_type"]]
                ]
                positions[raw["entry_type"]] += 1
                previous_mapping = existing_entry_mappings.get(raw["import_entry_id"])
                if previous_mapping and (
                    entry["sense_id"] != previous_mapping["sense_id"]
                    or entry["source_id"] != previous_mapping["source_id"]
                ):
                    raise AppError(
                        "IMPORT_ENTRY_IDENTITY_CHANGE_REQUIRED",
                        "义项或来源身份变化须使用新导入条目键,不能覆盖原映射",
                        409,
                    )
                fingerprint = canonical_hash([entry["source_locator"], entry["source_sentence"]])
                await session.execute(
                    text(
                        "INSERT INTO "
                        "import_entry_mapping(namespace,content_type,content_id,"
                        "import_entry_id,entry_id,"
                        "sense_id,source_id,entry_type,source_fingerprint,input_hash) "
                        "VALUES(:ns,:type,:content,:external,"
                        ":entry,:sense,:source,:kind,:fingerprint,:hash) ON DUPLICATE KEY UPDATE "
                        "sense_id=VALUES(sense_id),source_id=VALUES(source_id),"
                        "source_fingerprint=VALUES(source_fingerprint),"
                        "input_hash=VALUES(input_hash),version=version+1"
                    ),
                    {
                        "ns": IMPORT_NAMESPACE,
                        "type": kind,
                        "content": identity,
                        "external": raw["import_entry_id"],
                        "entry": entry["entry_id"],
                        "sense": entry["sense_id"],
                        "source": entry["source_id"],
                        "kind": raw["entry_type"],
                        "fingerprint": fingerprint,
                        "hash": canonical_hash(raw),
                    },
                )
            result = {"content_id": identity, "revision_id": saved.id}
        await session.execute(
            text(
                "INSERT INTO "
                "import_content_mapping(namespace,content_type,import_content_id,"
                "content_id,input_hash) "
                "VALUES(:ns,:type,:external,:content,:hash) ON DUPLICATE KEY UPDATE "
                "input_hash=VALUES(input_hash),version=version+1"
            ),
            {
                "ns": IMPORT_NAMESPACE,
                "type": kind,
                "external": item["import_content_id"],
                "content": identity,
                "hash": item["input_hash"],
            },
        )
        return result

    async def cancel(self, batch_id: str, actor: str) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            await self.batch(session, batch_id, True)
            await session.execute(
                text(
                    "UPDATE content_import_batch SET "
                    "status='CANCELLED',cancelled_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
                ),
                {"id": batch_id},
            )
            await self.stages.cancel_item_within(session, batch_id)
            return {"batch_id": batch_id, "status": "CANCELLED"}

    async def queue_confirmation(
        self, batch_id: str, payload: dict[str, Any], actor: str, key: str
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            return await self.queue_confirmation_within(session, batch_id, payload, actor, key)

    async def queue_confirmation_within(
        self, session: AsyncSession, batch_id: str, payload: dict[str, Any], actor: str, key: str
    ) -> dict[str, Any]:
        request_hash = canonical_hash([batch_id, payload])
        batch = await self.batch(session, batch_id, True)
        replay = await self.production.creation_replay(
            session, "v15.import.enqueue", actor, key, request_hash
        )
        if replay:
            return replay
        if batch["status"] == "CANCELLED":
            raise AppError("IMPORT_CANCELLED", "批次已经取消", 409)
        if batch["status"] not in {"PREVIEW_READY", "PARTIAL", "CONFIRMED"}:
            raise AppError("IMPORT_PREVIEW_NOT_READY", "批次尚未完成校验", 409)
        preview = decode(batch["manifest"]).get("preview", {})
        if (
            preview.get("hash") != payload["preview_hash"]
            or batch["preview_version"] != payload["preview_version"]
        ):
            raise AppError("IMPORT_PREVIEW_CHANGED", "请重新检查预览", 409)
        stage = await self.stages.ensure_within(
            session,
            batch_id,
            "CONFIRM",
            canonical_hash([batch_id, payload, actor, key]),
            "v15.1",
        )
        await session.execute(
            text(
                "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,payload) "
                "VALUES(:event,'CONTENT_IMPORT_CONFIRM',:id,:payload)"
            ),
            {
                "event": new_ulid(datetime.now(UTC)),
                "id": batch_id,
                "payload": encode(
                    {
                        "batch_id": batch_id,
                        "stage_id": stage,
                        "actor": actor,
                        "key": key,
                        "confirmation": payload,
                    }
                ),
            },
        )
        result = {"batch_id": batch_id, "stage_id": stage, "status": "PENDING"}
        await self.production.remember_creation(
            session, "v15.import.enqueue", actor, key, request_hash, result, datetime.now(UTC)
        )
        return result

    async def assert_claim_within(self, session: AsyncSession, claim: Claim, item_id: str) -> None:
        row = (
            await session.execute(
                text(
                    "SELECT public_id FROM content_import_stage WHERE public_id=:id "
                    "AND status='RUNNING' AND fencing_version=:fence AND lease_owner=:owner "
                    "AND input_hash=:hash AND config_version=:config AND item_id=:item "
                    "AND lease_until>UTC_TIMESTAMP(6) FOR UPDATE"
                ),
                {
                    "id": claim.id,
                    "fence": claim.fencing_version,
                    "owner": claim.owner,
                    "hash": claim.input_hash,
                    "config": claim.config_version,
                    "item": item_id,
                },
            )
        ).first()
        if not row:
            raise AppError("IMPORT_STAGE_LEASE_LOST", "阶段租约已变化", 409)
