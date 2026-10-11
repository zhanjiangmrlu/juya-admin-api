"""Legacy independent media ZIPs: suggestions never become bindings without manual selection."""

import asyncio
import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import decode, encode
from juya_admin_api.modules.content.schemas import AudioReference, SceneContent, SceneEntry
from juya_admin_api.modules.content_import.parser import canonical_hash, read_container
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

Invalidate = Callable[[AsyncSession, str], Awaitable[None]]
NAMESPACE = "v15.legacy-materials"
IMAGE_ROLES = {"COVER_IMAGE", "LEARNING_IMAGE", "WORD_IMAGE"}
AUDIO_ROLES = {"WHOLE_AUDIO", "FULL_AUDIO", "WORD_AUDIO", "CHUNK_AUDIO"}


def normalized_title(value: str) -> str:
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def material_manifest(
    files: Mapping[str, bytes], targets: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    resources = []
    titles: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        title = normalized_title(str(target.get("title_en") or ""))
        if title:
            titles.setdefault(title, []).append(target)
    for path in files:
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            media_type = "images"
            roles = ["COVER_IMAGE"] if kind == "short_reading" else sorted(IMAGE_ROLES)
        elif suffix in {".mp3", ".m4a", ".wav", ".aac"}:
            media_type = "audio"
            roles = (
                ["FULL_AUDIO"]
                if kind == "short_reading"
                else ["WHOLE_AUDIO", "WORD_AUDIO", "CHUNK_AUDIO"]
            )
        else:
            raise AppError("MATERIAL_FILE_TYPE_INVALID", "独立素材包仅接受图片和适用音频", 422)
        name = PurePosixPath(path).name
        title = normalized_title(PurePosixPath(path).stem)
        matches = titles.get(title, [])
        resources.append(
            {
                "path": path,
                "filename": name,
                "kind": media_type,
                "sha256": hashlib.sha256(files[path]).hexdigest(),
                "allowed_roles": roles,
                "matching_candidates": matches[:5],
                "matching_candidate_count": len(matches),
                "suggestions_truncated": len(matches) > 5,
                "ambiguous_title": len(matches) > 1,
                "requires_manual_selection": True,
            }
        )
    return resources


def validate_bindings(
    content: SceneContent | ReadingContent, bindings: list[dict[str, Any]]
) -> None:
    if not bindings or len(bindings) > 1000:
        raise AppError("MATERIAL_BINDINGS_INVALID", "请选择1至1000条素材绑定", 422)
    seen = set()
    for binding in bindings:
        role, entry_id = binding["role"], binding.get("entry_id")
        if binding.get("sentence_id"):
            raise AppError("MATERIAL_SENTENCE_ROLE_INVALID", "完整音频包不绑定伪造的单句切片", 422)
        if (role, entry_id) in seen:
            raise AppError("MATERIAL_DUPLICATE_TARGET", "同一目标角色不能绑定多份素材", 422)
        seen.add((role, entry_id))
        if isinstance(content, ReadingContent):
            if role not in {"COVER_IMAGE", "FULL_AUDIO"} or entry_id:
                raise AppError("MATERIAL_READING_ROLE_INVALID", "短文仅接受封面和全文音频", 422)
        elif role in {"WORD_IMAGE", "WORD_AUDIO", "CHUNK_AUDIO"}:
            group = content.chunks if role == "CHUNK_AUDIO" else content.vocabulary
            if not entry_id or not any(entry.entry_id == entry_id for entry in group):
                raise AppError("MATERIAL_ENTRY_NOT_FOUND", "角色与本篇稳定词条ID不对应", 422)
        elif role not in {"COVER_IMAGE", "LEARNING_IMAGE", "WHOLE_AUDIO"} or entry_id:
            raise AppError("MATERIAL_ROLE_INVALID", "素材角色无效或不接受词条ID", 422)


def apply_bindings(
    content: SceneContent | ReadingContent, bindings: list[dict[str, Any]]
) -> SceneContent | ReadingContent:
    validate_bindings(content, bindings)
    result = content.model_copy(deep=True)
    for binding in bindings:
        role, media = binding["role"], binding["media"]
        if role == "COVER_IMAGE":
            result.cover_asset_id = media["asset_id"]
        elif isinstance(result, ReadingContent):
            # New full audio requires a new human listening confirmation.
            result.audio = ReadingContent.model_validate(
                {
                    **result.model_dump(),
                    "audio": {
                        **media,
                        "listening_confirmed": False,
                        "confirmed_asset_id": None,
                        "confirmed_version_id": None,
                    },
                }
            ).audio
        elif role == "LEARNING_IMAGE":
            result.original_image_asset_id = media["asset_id"]
        elif role == "WHOLE_AUDIO":
            result.audio = AudioReference.model_validate(media)
            for sentence in result.dialogue:
                sentence.start_ms = sentence.end_ms = None
                sentence.timing_confirmed = False
                sentence.boundary_verified_by = ""
                sentence.audio_version_id = None
                sentence.slice_target_id = sentence.slice_version = (
                    sentence.slice_dependency_hash
                ) = None
        else:
            group = result.chunks if role == "CHUNK_AUDIO" else result.vocabulary
            entry: SceneEntry = next(
                entry for entry in group if entry.entry_id == binding["entry_id"]
            )
            if role == "WORD_IMAGE":
                entry.icon_asset_id = media["asset_id"]
                entry.confirmed = False
            else:
                entry.audio_target_id, entry.audio_version_id = (
                    media["target_id"],
                    media["version_id"],
                )
                entry.pronunciation_target_id = None
                entry.confirmed = False
    return result


class LegacyMaterialService:
    def __init__(
        self, imports: ImportService, readings: ShortReadingStore, invalidate: Invalidate
    ) -> None:
        self.imports, self.readings, self.invalidate = imports, readings, invalidate
        self.sessions, self.stages = imports.sessions, imports.stages

    async def create(
        self, series_id: str, content_type: str, data: bytes, actor: str, key: str
    ) -> dict[str, Any]:
        if content_type not in {"dialogue", "word_card", "short_reading"}:
            raise AppError("MATERIAL_TYPE_INVALID", "请选择明确内容模板", 422)
        if not data or len(data) > self.imports.limits.max_package_bytes:
            raise AppError("MATERIAL_PACKAGE_LIMIT", "素材包为空或超过限制", 422)
        digest = hashlib.sha256(data).hexdigest()
        request_hash = canonical_hash([series_id, content_type, digest])
        asset, identifier = uuid4().hex, new_ulid(datetime.now(UTC))
        self.imports.storage.mkdir(parents=True, exist_ok=True)
        artifact = self.imports.path(asset)
        await asyncio.to_thread(artifact.write_bytes, data)
        try:
            async with self.sessions() as session, session.begin():
                if not await session.scalar(
                    text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                    {"id": series_id},
                ):
                    raise AppError("SERIES_NOT_FOUND", "分类不存在", 404)
                replay = await self.imports.production.creation_replay(
                    session, "v15.materials.create", actor, key, request_hash
                )
                if replay:
                    artifact.unlink(missing_ok=True)
                    return replay
                await session.execute(
                    text(
                        "INSERT INTO content_import_batch(public_id,namespace,series_id,"
                        "content_type,import_mode,asset_id,input_hash,status,created_by) "
                        "VALUES(:id,:namespace,:series,:type,'MATERIAL_ZIP',:asset,:hash,"
                        "'PENDING',:actor)"
                    ),
                    {
                        "id": identifier,
                        "namespace": NAMESPACE,
                        "series": series_id,
                        "type": content_type,
                        "asset": asset,
                        "hash": digest,
                        "actor": actor,
                    },
                )
                stage = await self.stages.ensure_within(
                    session, identifier, "MATERIAL_VALIDATE", digest, "v15.materials.1"
                )
                result = {"batch_id": identifier, "stage_id": stage, "status": "PENDING"}
                await self.imports.production.remember_creation(
                    session,
                    "v15.materials.create",
                    actor,
                    key,
                    request_hash,
                    result,
                    datetime.now(UTC),
                )
            await self.stages.enqueue(stage, datetime.now(UTC))
            return result
        except BaseException:
            # If SQL committed, retain the durable original for recovery after an enqueue failure.
            async with self.sessions() as session:
                exists = await session.scalar(
                    text("SELECT id FROM content_import_batch WHERE public_id=:id"),
                    {"id": identifier},
                )
            if not exists:
                artifact.unlink(missing_ok=True)
            raise

    async def _batch(
        self, session: AsyncSession, identifier: str, lock: bool = False
    ) -> dict[str, Any]:
        row = await self.imports.batch(session, identifier, lock)
        if row["namespace"] != NAMESPACE:
            raise AppError("MATERIAL_BATCH_NOT_FOUND", "不是独立素材批次", 404)
        return row

    async def preview(self, identifier: str) -> dict[str, Any]:
        async with self.sessions() as session:
            batch = await self._batch(session, identifier)
            return {
                "batch_id": identifier,
                "status": batch["status"],
                "input_hash": batch["input_hash"],
                "manifest": decode(batch["manifest"]),
            }

    async def _draft(
        self, session: AsyncSession, batch: dict[str, Any], revision_id: str, expected_version: int
    ) -> dict[str, Any]:
        body: SceneContent | ReadingContent
        if batch["content_type"] == "short_reading":
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT r.*,c.public_id AS content_id FROM short_reading_revision "
                            "r JOIN short_reading c ON c.draft_revision_id=r.public_id "
                            "AND r.short_reading_id=c.public_id WHERE "
                            "r.public_id=:id AND c.series_id=:series FOR UPDATE"
                        ),
                        {"id": revision_id, "series": batch["series_id"]},
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["status"] != "DRAFT":
                raise AppError("MATERIAL_DRAFT_REQUIRED", "仅当前短文草稿可绑定", 409)
            body = ReadingContent.model_validate(decode(row["content_snapshot"]))
            target = {
                "content_id": row["content_id"],
                "revision_id": revision_id,
                "version": int(row["edit_version"]),
                "body": body,
            }
        else:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT r.*,s.public_id AS content_id FROM scene_revision r JOIN "
                            "scene s ON s.draft_revision_id=r.id JOIN content_series cs ON "
                            "cs.id=s.series_id WHERE r.public_id=:id AND cs.public_id=:series "
                            "FOR UPDATE"
                        ),
                        {"id": revision_id, "series": batch["series_id"]},
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["status"] != "DRAFT":
                raise AppError("MATERIAL_DRAFT_REQUIRED", "已发布版本须先创建新草稿", 409)
            body = SceneContent.model_validate(decode(row["content_snapshot"]))
            kind = "word_card" if body.template_type == "vocabulary" else body.template_type
            if kind != batch["content_type"]:
                raise AppError("MATERIAL_TYPE_INVALID", "素材批次与草稿模板不同", 409)
            target = {
                "content_id": row["content_id"],
                "revision_id": revision_id,
                "version": int(row["edit_version"]),
                "body": body,
            }
        if target["version"] != expected_version:
            raise AppError("REVISION_VERSION_CONFLICT", "目标草稿已更新", 409)
        return target

    async def queue_bind(
        self,
        batch_id: str,
        revision_id: str,
        expected_version: int,
        manual_checked: bool,
        bindings: list[dict[str, Any]],
        actor: str,
        key: str,
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            return await self.queue_bind_within(
                session,
                batch_id,
                revision_id,
                expected_version,
                manual_checked,
                bindings,
                actor,
                key,
            )

    async def queue_bind_within(
        self,
        session: AsyncSession,
        batch_id: str,
        revision_id: str,
        expected_version: int,
        manual_checked: bool,
        bindings: list[dict[str, Any]],
        actor: str,
        key: str,
    ) -> dict[str, Any]:
        if manual_checked is not True:
            raise AppError("MATERIAL_MANUAL_CHECK_REQUIRED", "必须逐项人工核对角色与来源", 422)
        request = {
            "revision_id": revision_id,
            "expected_version": expected_version,
            "bindings": bindings,
        }
        request_hash = canonical_hash([batch_id, request])
        batch = await self._batch(session, batch_id, True)
        replay = await self.imports.production.creation_replay(
            session, "v15.materials.enqueue", actor, key, request_hash
        )
        if replay:
            return replay
        if batch["status"] not in {"PREVIEW_READY", "BOUND"}:
            raise AppError("MATERIAL_PREVIEW_NOT_READY", "素材校验未完成或已取消", 409)
        target = await self._draft(session, batch, revision_id, expected_version)
        validate_bindings(target["body"], bindings)
        manifest = decode(batch["manifest"])
        resources = {resource["path"]: resource for resource in manifest["resources"]}
        for binding in bindings:
            resource = resources.get(binding["path"])
            if resource is None or binding["role"] not in resource["allowed_roles"]:
                raise AppError("MATERIAL_ROLE_INVALID", "路径不存在或角色不适用", 422)
        execution_hash = canonical_hash([request_hash, actor, key])
        name = "MATERIAL_BIND_" + hashlib.sha256(revision_id.encode()).hexdigest()[:12]
        stage = await self.stages.ensure_within(
            session, batch_id, name, execution_hash, "v15.materials.1"
        )
        manifest.setdefault("binding_requests", {})[stage] = {
            "request": request,
            "actor": actor,
            "key": key,
            "input_hash": execution_hash,
        }
        await session.execute(
            text("UPDATE content_import_batch SET manifest=:manifest WHERE public_id=:id"),
            {"id": batch_id, "manifest": encode(manifest)},
        )
        result = {"batch_id": batch_id, "stage_id": stage, "status": "PENDING"}
        await self.imports.production.remember_creation(
            session,
            "v15.materials.enqueue",
            actor,
            key,
            request_hash,
            result,
            datetime.now(UTC),
        )
        await self.stages.enqueue_within(session, stage, datetime.now(UTC))
        return result

    async def _renew(self, claim: Claim) -> None:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE content_import_stage SET "
                    "lease_until=UTC_TIMESTAMP(6)+INTERVAL 180 SECOND WHERE "
                    "public_id=:id AND status='RUNNING' AND fencing_version=:fence "
                    "AND lease_owner=:owner AND lease_until>UTC_TIMESTAMP(6)"
                ),
                {"id": claim.id, "fence": claim.fencing_version, "owner": claim.owner},
            )
            if not getattr(result, "rowcount", 0):
                raise AppError("IMPORT_STAGE_LEASE_LOST", "素材阶段租约已失效", 409)

    async def run(self, stage_id: str, owner: str, now: datetime) -> dict[str, Any] | None:
        claim = await self.stages.claim(stage_id, owner, now)
        if claim is None:
            return None
        async with self.sessions() as session:
            stage = (
                await session.execute(
                    text("SELECT item_id,stage FROM content_import_stage WHERE public_id=:id"),
                    {"id": stage_id},
                )
            ).one()
            batch = await self._batch(session, stage.item_id)
        files: Mapping[str, bytes] | None = None
        try:
            files = await asyncio.to_thread(
                read_container, await self.imports.original(batch), self.imports.limits, spool=True
            )
            if stage.stage == "MATERIAL_VALIDATE":
                async with self.sessions() as session:
                    if batch["content_type"] == "short_reading":
                        rows = (
                            (
                                await session.execute(
                                    text(
                                        "SELECT r.public_id AS revision_id,r.edit_version,"
                                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,"
                                        "'$.title_en')) AS "
                                        "title_en FROM short_reading_revision r JOIN "
                                        "short_reading c ON "
                                        "c.draft_revision_id=r.public_id WHERE "
                                        "c.series_id=:series AND "
                                        "r.status='DRAFT'"
                                    ),
                                    {"series": batch["series_id"]},
                                )
                            )
                            .mappings()
                            .all()
                        )
                    else:
                        rows = (
                            (
                                await session.execute(
                                    text(
                                        "SELECT r.public_id AS revision_id,r.edit_version,"
                                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,"
                                        "'$.title_en')) AS "
                                        "title_en FROM scene_revision r JOIN scene s ON "
                                        "s.draft_revision_id=r.id JOIN content_series cs ON "
                                        "cs.id=s.series_id JOIN content_template t ON "
                                        "t.id=s.template_id "
                                        "WHERE cs.public_id=:series AND r.status='DRAFT' AND "
                                        "t.template_type=:type"
                                    ),
                                    {
                                        "series": batch["series_id"],
                                        "type": "vocabulary"
                                        if batch["content_type"] == "word_card"
                                        else "dialogue",
                                    },
                                )
                            )
                            .mappings()
                            .all()
                        )
                resources = await asyncio.to_thread(
                    material_manifest,
                    files,
                    [{**dict(row), "edit_version": int(row["edit_version"])} for row in rows],
                    batch["content_type"],
                )
                if not resources:
                    raise AppError("MATERIAL_PACKAGE_EMPTY", "素材包没有有效文件", 422)
                result = {"batch_id": batch["public_id"], "status": "PREVIEW_READY"}
                manifest: dict[str, Any] = {"resources": resources, "binding_requests": {}}
            else:
                manifest = decode(batch["manifest"])
                queued = manifest["binding_requests"][stage_id]
                if queued["input_hash"] != claim.input_hash:
                    raise AppError("MATERIAL_REQUEST_CHANGED", "绑定请求已变化", 409)
                request = queued["request"]
                async with self.sessions() as session, session.begin():
                    current = await self._batch(session, batch["public_id"], True)
                    await self.imports.assert_claim_within(session, claim, batch["public_id"])
                    if current["status"] == "CANCELLED":
                        raise AppError("IMPORT_CANCELLED", "素材批次已取消", 409)
                    target = await self._draft(
                        session, current, request["revision_id"], request["expected_version"]
                    )
                    validate_bindings(target["body"], request["bindings"])
                by_path = {resource["path"]: resource for resource in manifest["resources"]}
                prepared = []
                for binding in request["bindings"]:
                    await self._renew(claim)
                    resource = by_path[binding["path"]]
                    data = files[binding["path"]]
                    if hashlib.sha256(data).hexdigest() != resource["sha256"]:
                        raise AppError("IMPORT_ARTIFACT_CHANGED", "素材字节已变化", 409)
                    media = await self.imports.bind_media(
                        data, resource["kind"], batch["created_by"], binding["role"]
                    )
                    prepared.append({**binding, "media": media})
                result = {"batch_id": batch["public_id"], "status": "BOUND"}
            async with self.sessions() as session, session.begin():
                current = await self._batch(session, batch["public_id"], True)
                await self.imports.assert_claim_within(session, claim, batch["public_id"])
                if current["status"] == "CANCELLED":
                    raise AppError("IMPORT_CANCELLED", "素材批次已取消", 409)
                if stage.stage != "MATERIAL_VALIDATE":
                    target = await self._draft(
                        session, current, request["revision_id"], request["expected_version"]
                    )
                    body = apply_bindings(target["body"], prepared)
                    if isinstance(body, ReadingContent):
                        reading = await self.readings.row(session, target["content_id"], True)
                        await self.readings.mutate(
                            session,
                            "save",
                            reading,
                            queued["actor"],
                            {
                                "revision_id": target["revision_id"],
                                "expected_edit_version": target["version"],
                                "content": body.model_dump(mode="json"),
                            },
                        )
                        version = target["version"] + 1
                    else:
                        revision = SceneRevision(
                            target["revision_id"],
                            target["content_id"],
                            None,
                            version=target["version"],
                            content=body.model_dump(mode="json"),
                            created_by=queued["actor"],
                        )
                        saved = await self.imports.production.save_revision_within(
                            session, revision, target["version"]
                        )
                        version = saved.version
                    await self.invalidate(session, target["revision_id"])
                    result.update({"revision_id": target["revision_id"], "edit_version": version})
                    # Keep parallel bindings to other revisions and their results.
                    manifest = decode(current["manifest"])
                    manifest.setdefault("binding_results", {})[stage_id] = result
                    await session.execute(
                        text(
                            "INSERT INTO audit_event(public_id,actor_public_id,action,"
                            "object_type,object_public_id,after_summary,request_id) "
                            "VALUES(:id,:actor,'content.materials.bind','revision',:revision,"
                            ":summary,:request)"
                        ),
                        {
                            "id": new_ulid(datetime.now(UTC)),
                            "actor": queued["actor"],
                            "revision": target["revision_id"],
                            "summary": encode(result),
                            "request": queued["key"],
                        },
                    )
                await self.imports.assert_claim_within(session, claim, batch["public_id"])
                await session.execute(
                    text(
                        "UPDATE content_import_batch SET manifest=:manifest,"
                        "status=:status WHERE public_id=:id"
                    ),
                    {
                        "id": batch["public_id"],
                        "manifest": encode(manifest),
                        "status": result["status"],
                    },
                )
                await session.execute(
                    text(
                        "UPDATE content_import_stage SET status='SUCCEEDED',"
                        "output=:output,lease_owner=NULL,lease_until=NULL WHERE public_id=:id"
                    ),
                    {"id": stage_id, "output": encode(result)},
                )
            return result
        except (AppError, ValueError) as error:
            code = error.code if isinstance(error, AppError) else "MATERIAL_CONTENT_INVALID"
            if code not in {"IMPORT_STAGE_LEASE_LOST", "IMPORT_CANCELLED"}:
                async with self.sessions() as session, session.begin():
                    current = await self._batch(session, batch["public_id"], True)
                    await self.imports.assert_claim_within(session, claim, batch["public_id"])
                    if current["status"] == "CANCELLED":
                        raise AppError("IMPORT_CANCELLED", "素材批次已取消", 409) from error
                    manifest = decode(current["manifest"])
                    manifest.setdefault("binding_errors", {})[stage_id] = {"error_code": code}
                    await session.execute(
                        text(
                            "UPDATE content_import_batch SET manifest=:manifest,"
                            "status=:status WHERE public_id=:id"
                        ),
                        {
                            "id": batch["public_id"],
                            "manifest": encode(manifest),
                            "status": "FAILED"
                            if stage.stage == "MATERIAL_VALIDATE"
                            else current["status"],
                        },
                    )
                    await session.execute(
                        text(
                            "UPDATE content_import_stage SET status='FAILED',error_code=:code,"
                            "lease_owner=NULL,lease_until=NULL WHERE public_id=:id"
                        ),
                        {"id": stage_id, "code": code},
                    )
            raise
        finally:
            close = getattr(files, "close", None)
            if close:
                close()

    async def cancel(self, batch_id: str, actor: str) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            await self._batch(session, batch_id, True)
            await session.execute(
                text(
                    "UPDATE content_import_batch SET status='CANCELLED',"
                    "cancelled_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
                ),
                {"id": batch_id},
            )
            await self.stages.cancel_item_within(session, batch_id)
        return {"batch_id": batch_id, "status": "CANCELLED"}
