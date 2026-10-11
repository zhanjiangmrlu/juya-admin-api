"""Human-only confirmation and durable sentence slicing for modern scene drafts."""

import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.audio_pipeline.service import SliceInput
from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_entries import details
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.modules.pronunciation.models import targets
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

PrepareSlice = Callable[[str, str], Awaitable[None]]
SignSlice = Callable[[str, int], Awaitable[str]]


def validate_boundary(start_ms: int, end_ms: int, duration_ms: int, manual: bool) -> Boundary:
    if manual is not True or duration_ms <= 0 or end_ms > duration_ms:
        raise AppError("SENTENCE_BOUNDARY_INVALID", "边界须人工检查且不能超过实际音频时长", 422)
    return Boundary(start_ms, end_ms)


class SceneAuthoringStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        proofreading: ProofreadingStore,
        stages: StageStore,
        prepare_slice: PrepareSlice | None = None,
        encoding_config: dict[str, object] | None = None,
        sign_slice: SignSlice | None = None,
    ):
        self.sessions = sessions
        self.proofreading = proofreading
        self.stages = stages
        self.prepare_slice = prepare_slice
        self.encoding_config = encoding_config or FFmpegSlicer.encoding_config
        self.sign_slice = sign_slice

    async def locked_revision(
        self, session: AsyncSession, revision_id: str, expected_version: int | None = None
    ) -> tuple[dict[str, Any], SceneContent]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT r.public_id,r.edit_version,r.status,r.content_snapshot,"
                        "c.public_id AS content_id "
                        "FROM scene c JOIN scene_revision r ON r.scene_id=c.id "
                        "WHERE r.public_id=:revision AND c.draft_revision_id=r.id FOR UPDATE"
                    ),
                    {"revision": revision_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] not in ("DRAFT", "REVIEWED", "PUBLISH_READY"):
            raise AppError("AUTHORING_REVISION_INVALID", "仅当前未发布草稿可人工确认", 409)
        if expected_version is not None and row["edit_version"] != expected_version:
            raise AppError("REVISION_VERSION_CONFLICT", "草稿版本已变化", 409)
        snapshot = row["content_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        content = SceneContent.model_validate(snapshot)
        if content.schema_version != 2:
            raise AppError("MODERN_SCENE_REQUIRED", "请先转换为V1.5场景草稿", 409)
        return dict(row), content

    async def persist(
        self,
        session: AsyncSession,
        row: dict[str, Any],
        content: SceneContent,
        actor: str,
        action: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        await session.execute(
            text(
                "UPDATE scene_revision SET content_snapshot=:snapshot,"
                "edit_version=edit_version+1,status='DRAFT' WHERE public_id=:revision"
            ),
            {"snapshot": content.model_dump_json(), "revision": row["public_id"]},
        )
        await self.proofreading.invalidate_within(session, row["public_id"])
        await self.proofreading.audit(session, row["public_id"], actor, action, summary)
        return {
            "revision_id": row["public_id"],
            "edit_version": row["edit_version"] + 1,
            "content": content.model_dump(),
        }

    async def source(self, session: AsyncSession, content: SceneContent) -> dict[str, Any]:
        if content.audio is None:
            raise AppError("SOURCE_AUDIO_REQUIRED", "场景需要固定原音频", 409)
        row = (
            (
                await session.execute(
                    text(
                        "SELECT v.public_id AS version_id,v.status,t.target_type,"
                        "m.public_id AS asset_id,m.sha256,"
                        "m.object_key,m.duration_ms,m.status AS asset_status,m.security_status "
                        "FROM audio_version v JOIN media_asset m ON m.id=v.asset_id "
                        "JOIN audio_target t ON t.id=v.target_id WHERE v.public_id=:version "
                        "AND t.public_id=:target AND m.public_id=:asset FOR UPDATE"
                    ),
                    {
                        "version": content.audio.version_id,
                        "target": content.audio.target_id,
                        "asset": content.audio.asset_id,
                    },
                )
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["target_type"]
            not in {"WHOLE", "SCENE", "scene", "whole", "dialogue", "DIALOGUE"}
            or row["status"] not in ("CONFIRMED", "ACTIVE", "SUPERSEDED")
            or row["asset_status"] != "CONFIRMED"
            or row["security_status"] != "PASSED"
            or not row["object_key"]
            or not row["sha256"]
            or not row["duration_ms"]
        ):
            raise AppError("SOURCE_AUDIO_NOT_READY", "原音频须可解码且已通过审核与确认", 409)
        return dict(row)

    async def confirm_entry(
        self, revision_id: str, entry_id: str, expected_version: int, actor: str, manual: bool
    ) -> dict[str, Any]:
        if manual is not True:
            raise AppError("ENTRY_MANUAL_CHECK_REQUIRED", "必须人工检查条目释义与来源", 422)
        async with self.sessions() as session, session.begin():
            return await self.confirm_entry_within(
                session, revision_id, entry_id, expected_version, actor, manual
            )

    async def confirm_entry_within(
        self,
        session: AsyncSession,
        revision_id: str,
        entry_id: str,
        expected_version: int,
        actor: str,
        manual: bool,
    ) -> dict[str, Any]:
        if manual is not True:
            raise AppError("ENTRY_MANUAL_CHECK_REQUIRED", "必须人工检查条目释义与来源", 422)
        row, content = await self.locked_revision(session, revision_id, expected_version)
        word = next((item for item in content.vocabulary if item.entry_id == entry_id), None)
        entry = word or next((item for item in content.chunks if item.entry_id == entry_id), None)
        if entry is None or not entry.english.strip() or not entry.chinese.strip():
            raise AppError("ENTRY_CONFIRMATION_INVALID", "条目英文与本篇释义必填", 422)
        saved = (
            (
                await session.execute(
                    text(
                        "SELECT details_hash,sense_id,source_id FROM content_entry_revision "
                        "WHERE revision_id=:revision AND entry_id=:entry FOR UPDATE"
                    ),
                    {"revision": revision_id, "entry": entry_id},
                )
            )
            .mappings()
            .first()
        )
        if (
            saved is None
            or (saved["sense_id"], saved["source_id"]) != (entry.sense_id, entry.source_id)
            or saved["details_hash"] != snapshot_hash(details(entry))
        ):
            raise AppError("ENTRY_IDENTITY_CONFLICT", "条目义项或来源与服务端版本不符", 409)
        uploaded_target = entry.audio_target_id
        uploaded_version = entry.audio_version_id
        if bool(uploaded_target) != bool(uploaded_version):
            raise AppError("ENTRY_AUDIO_INVALID", "上传音频必须固定目标与资源版本", 422)
        if uploaded_version:
            audio = (
                (
                    await session.execute(
                        text(
                            "SELECT v.status,t.target_type,m.status AS asset_status,"
                            "m.security_status,m.object_key "
                            "FROM audio_version v JOIN audio_target t ON t.id=v.target_id "
                            "JOIN media_asset m ON m.id=v.asset_id WHERE v.public_id=:version "
                            "AND t.public_id=:target FOR UPDATE"
                        ),
                        {"version": uploaded_version, "target": uploaded_target},
                    )
                )
                .mappings()
                .first()
            )
            if (
                audio is None
                or audio["target_type"]
                not in (
                    ("WORD", "VOCABULARY", "vocabulary", "word")
                    if word is not None
                    else ("CHUNK", "PHRASE", "phrase", "chunk")
                )
                or audio["status"] not in ("CONFIRMED", "ACTIVE", "SUPERSEDED")
                or audio["asset_status"] != "CONFIRMED"
                or audio["security_status"] != "PASSED"
                or not audio["object_key"]
            ):
                raise AppError("ENTRY_AUDIO_NOT_READY", "条目上传音频未确认或未通过审核", 409)
        now = datetime.now(UTC)
        # New immutable target preserves all previously published/favorited fixed references.
        target_id = new_ulid(now)
        await session.execute(
            insert(targets).values(
                public_id=target_id,
                confirmed_text=entry.pronunciation_text.strip() or entry.english,
                reading_variant=entry.reading_variant,
                language="en",
                target_type="WORD" if word is not None else "CHUNK",
                allow_synthesis=entry.allow_synthesis,
                uploaded_target_id=uploaded_target,
                uploaded_resource_version=uploaded_version,
                status="CONFIRMED",
                config_version="1",
                confirmed_by=actor,
                confirmed_at=int(time.time()),
                created_at=int(time.time()),
            )
        )
        entry.pronunciation_target_id = target_id
        entry.confirmed = True
        digest = snapshot_hash(details(entry))
        await session.execute(
            text(
                "UPDATE content_entry_revision SET details=:details,"
                "details_hash=:hash,confirmed_by=:actor,confirmed_at=:now "
                "WHERE revision_id=:revision AND entry_id=:entry"
            ),
            {
                "details": json.dumps(details(entry), ensure_ascii=False),
                "hash": digest,
                "actor": actor,
                "now": now.replace(tzinfo=None),
                "revision": revision_id,
                "entry": entry_id,
            },
        )
        await session.execute(
            text(
                "UPDATE content_sense SET confirmed_by=:actor,confirmed_at=:now "
                "WHERE public_id=:sense AND entry_id=:entry"
            ),
            {
                "actor": actor,
                "now": now.replace(tzinfo=None),
                "sense": entry.sense_id,
                "entry": entry_id,
            },
        )
        return await self.persist(
            session,
            row,
            content,
            actor,
            "content.entry.confirm",
            {"entry_id": entry_id, "pronunciation_target_id": target_id},
        )

    async def confirm_entries_bulk(
        self,
        candidates: list[dict[str, Any]],
        actor: str,
        manual: bool,
    ) -> dict[str, Any]:
        if manual is not True or not 1 <= len(candidates) <= 1000:
            raise AppError(
                "ENTRY_BATCH_CONFIRMATION_REQUIRED", "须显式批量核对且范围为1至1000篇", 422
            )
        if len({item["revision_id"] for item in candidates}) != len(candidates):
            raise AppError("ENTRY_BATCH_DUPLICATE", "批量版本不能重复", 422)
        results = []
        for candidate in candidates:
            revision_id = candidate["revision_id"]
            try:
                async with self.sessions() as session, session.begin():
                    row, content = await self.locked_revision(
                        session, revision_id, candidate["expected_version"]
                    )
                    if snapshot_hash(content.model_dump()) != candidate["candidate_hash"]:
                        raise AppError("ENTRY_BATCH_HASH_CHANGED", "批量确认的候选内容已变化", 409)
                    result = {
                        "revision_id": revision_id,
                        "edit_version": row["edit_version"],
                        "content": content.model_dump(),
                    }
                    for entry in [*content.vocabulary, *content.chunks]:
                        if not entry.confirmed:
                            result = await self.confirm_entry_within(
                                session,
                                revision_id,
                                entry.entry_id,
                                result["edit_version"],
                                actor,
                                True,
                            )
                    await self.proofreading.audit(
                        session,
                        revision_id,
                        actor,
                        "content.entries.bulk_confirm",
                        {
                            "candidate_hash": candidate["candidate_hash"],
                            "entry_ids": [
                                e.entry_id for e in [*content.vocabulary, *content.chunks]
                            ],
                        },
                    )
                    results.append({"status": "SUCCESS", **result})
            except AppError as error:
                results.append(
                    {"revision_id": revision_id, "status": "FAILED", "error_code": error.code}
                )
        return {
            "items": results,
            "status": "PARTIAL"
            if any(row["status"] == "FAILED" for row in results)
            else "CONFIRMED",
        }

    async def confirm_boundary(
        self,
        revision_id: str,
        sentence_id: str,
        expected_version: int,
        start_ms: int,
        end_ms: int,
        actor: str,
        manual: bool,
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            row, content = await self.locked_revision(session, revision_id, expected_version)
            source = await self.source(session, content)
            boundary = validate_boundary(start_ms, end_ms, source["duration_ms"], manual)
            sentence = next((item for item in content.dialogue if item.id == sentence_id), None)
            if sentence is None:
                raise AppError("SENTENCE_NOT_FOUND", "句子不存在", 404)
            sentence.start_ms, sentence.end_ms = boundary.start_ms, boundary.end_ms
            sentence.audio_version_id = source["version_id"]
            sentence.timing_confirmed, sentence.boundary_verified_by = True, actor
            sentence.slice_target_id = sentence.slice_version = sentence.slice_dependency_hash = (
                None
            )
            result = await self.persist(
                session,
                row,
                content,
                actor,
                "content.boundary.confirm",
                {"sentence_id": sentence_id, "start_ms": start_ms, "end_ms": end_ms},
            )
            item = self.make_input(row, content, sentence_id, source)
            digest = item.dependency(self.encoding_config)
            stage_item = snapshot_hash({"revision_id": revision_id, "sentence_id": sentence_id})
            stage_id = await self.stages.ensure_within(
                session, stage_item, "SENTENCE_SLICE", digest, snapshot_hash(self.encoding_config)
            )
            # Explicit boundary confirmation may retry local work or reattach a cached slice.
            # Charged provider stages are never reset by this command.
            await session.execute(
                text(
                    "UPDATE content_import_stage SET status='PENDING',"
                    "fencing_version=fencing_version+1,lease_owner=NULL,lease_until=NULL,"
                    "next_retry_at=NULL,error_code=NULL,output=NULL WHERE public_id=:id "
                    "AND stage='SENTENCE_SLICE' AND status IN ('FAILED','CANCELLED','SUCCEEDED')"
                ),
                {"id": stage_id},
            )
            await session.execute(
                text(
                    "INSERT INTO admin_outbox(event_id,event_type,"
                    "aggregate_public_id,payload) "
                    "VALUES(:event,'CONTENT_SENTENCE_SLICE',:revision,:payload)"
                ),
                {
                    "event": new_ulid(datetime.now(UTC)),
                    "revision": revision_id,
                    "payload": json.dumps(
                        {
                            "revision_id": revision_id,
                            "sentence_id": sentence_id,
                            "input_hash": digest,
                            "stage_id": stage_id,
                            "encoding_config": self.encoding_config,
                        }
                    ),
                },
            )
            return result | {
                "stage_id": stage_id,
                "slice_status": "PREPARING",
                "dependency_hash": digest,
            }

    def make_input(
        self, row: dict[str, Any], content: SceneContent, sentence_id: str, source: dict[str, Any]
    ) -> SliceInput:
        sentence = next((item for item in content.dialogue if item.id == sentence_id), None)
        if (
            sentence is None
            or not sentence.timing_confirmed
            or not sentence.boundary_verified_by
            or sentence.audio_version_id != source["version_id"]
            or sentence.start_ms is None
            or sentence.end_ms is None
        ):
            raise AppError("SENTENCE_BOUNDARY_UNCONFIRMED", "句子边界未人工确认或原音频已变化", 409)
        boundary = validate_boundary(
            sentence.start_ms, sentence.end_ms, source["duration_ms"], True
        )
        return SliceInput(
            row["public_id"],
            sentence_id,
            sentence.english,
            source["object_key"],
            source["version_id"],
            source["sha256"],
            boundary,
            sentence.boundary_verified_by,
            content_type="dialogue",
            content_id=row["content_id"],
        )

    async def locked_input(
        self, session: AsyncSession, revision_id: str, sentence_id: str
    ) -> SliceInput:
        row, content = await self.locked_revision(session, revision_id)
        return self.make_input(row, content, sentence_id, await self.source(session, content))

    async def current_input(self, revision_id: str, sentence_id: str) -> SliceInput:
        async with self.sessions() as session, session.begin():
            return await self.locked_input(session, revision_id, sentence_id)

    async def slice_preview(
        self, revision_id: str, sentence_id: str, dependency_hash: str
    ) -> dict[str, Any]:
        if self.prepare_slice is None or self.sign_slice is None:
            raise AppError("SLICE_PREVIEW_UNAVAILABLE", "切片试听服务尚未配置", 409)
        async with self.sessions() as session, session.begin():
            current = await self.locked_input(session, revision_id, sentence_id)
            if current.dependency(self.encoding_config) != dependency_hash:
                raise AppError("SLICE_DEPENDENCY_CHANGED", "原音频或句子边界已变化", 409)
            saved = (
                (
                    await session.execute(
                        text(
                            "SELECT asset_id,resource_version FROM sentence_audio_slice "
                            "WHERE revision_id=:revision AND sentence_id=:sentence "
                            "AND dependency_hash=:hash "
                            "AND status IN ('READY','CONFIRMED') FOR UPDATE"
                        ),
                        {"revision": revision_id, "sentence": sentence_id, "hash": dependency_hash},
                    )
                )
                .mappings()
                .first()
            )
            if saved is None:
                raise AppError("SLICE_NOT_READY", "切片尚未完成", 409)
            asset_id, version = saved["asset_id"], saved["resource_version"]
        await self.prepare_slice(asset_id, version)
        async with self.sessions() as session, session.begin():
            current = await self.locked_input(session, revision_id, sentence_id)
            if current.dependency(self.encoding_config) != dependency_hash:
                raise AppError("SLICE_DEPENDENCY_CHANGED", "试听准备期间原音频或句子已变化", 409)
            actual = (
                (
                    await session.execute(
                        text(
                            "SELECT s.target_id,s.resource_version,s.asset_id,"
                            "s.status AS slice_status,m.object_key,m.sha256,m.duration_ms,"
                            "m.content_type,m.status,m.security_status "
                            "FROM sentence_audio_slice s "
                            "JOIN media_asset m ON m.public_id=s.asset_id "
                            "JOIN audio_version v ON v.public_id=s.resource_version "
                            "AND v.asset_id=m.id "
                            "JOIN audio_target t ON t.id=v.target_id AND t.public_id=s.target_id "
                            "WHERE s.revision_id=:revision AND s.sentence_id=:sentence "
                            "AND s.dependency_hash=:hash AND s.asset_id=:asset "
                            "AND s.resource_version=:version "
                            "AND s.status IN ('READY','CONFIRMED') FOR UPDATE"
                        ),
                        {
                            "revision": revision_id,
                            "sentence": sentence_id,
                            "hash": dependency_hash,
                            "asset": asset_id,
                            "version": version,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if (
                actual is None
                or actual["status"] != "CONFIRMED"
                or actual["security_status"] != "PASSED"
                or not actual["duration_ms"]
                or not actual["object_key"].startswith("sealed/media/audio/")
            ):
                raise AppError("SLICE_SECURITY_REQUIRED", "切片须可解码且已通过安全审核", 409)
            url = await self.sign_slice(asset_id, 60)
            return {
                "url": url,
                "expires_in": 60,
                "asset_id": asset_id,
                "resource_version": version,
                "target_id": actual["target_id"],
                "dependency_hash": dependency_hash,
                "duration_ms": actual["duration_ms"],
                "content_type": actual["content_type"],
                "sha256": actual["sha256"],
                "status": actual["slice_status"],
            }

    async def confirm_slice(
        self,
        revision_id: str,
        sentence_id: str,
        expected_version: int,
        dependency_hash: str,
        actor: str,
        listened: bool,
    ) -> dict[str, Any]:
        if listened is not True:
            raise AppError("SLICE_LISTENING_REQUIRED", "必须实际试听该句切片", 422)
        # Decoder/security prepare runs outside the content transaction; slicing stays asynchronous.
        async with self.sessions() as session:
            saved = (
                (
                    await session.execute(
                        text(
                            "SELECT asset_id,resource_version FROM "
                            "sentence_audio_slice WHERE revision_id=:revision "
                            "AND sentence_id=:sentence "
                            "AND dependency_hash=:hash AND status IN ('READY','CONFIRMED')"
                        ),
                        {"revision": revision_id, "sentence": sentence_id, "hash": dependency_hash},
                    )
                )
                .mappings()
                .first()
            )
        if saved is None or self.prepare_slice is None:
            raise AppError("SLICE_NOT_READY", "切片尚未完成或安全检查未配置", 409)
        await self.prepare_slice(saved["asset_id"], saved["resource_version"])
        async with self.sessions() as session, session.begin():
            row, content = await self.locked_revision(session, revision_id, expected_version)
            current = self.make_input(
                row, content, sentence_id, await self.source(session, content)
            )
            if current.dependency(self.encoding_config) != dependency_hash:
                raise AppError("SLICE_DEPENDENCY_CHANGED", "原音频或句子边界已变化", 409)
            audio = (
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id,s.target_id,s.resource_version,"
                            "s.asset_id,m.status,m.security_status,m.object_key,m.duration_ms "
                            "FROM sentence_audio_slice s "
                            "JOIN media_asset m ON m.public_id=s.asset_id "
                            "JOIN audio_version v ON v.public_id=s.resource_version "
                            "AND v.asset_id=m.id "
                            "JOIN audio_target t ON t.id=v.target_id AND t.public_id=s.target_id "
                            "WHERE s.revision_id=:revision AND s.sentence_id=:sentence "
                            "AND s.dependency_hash=:hash FOR UPDATE"
                        ),
                        {"revision": revision_id, "sentence": sentence_id, "hash": dependency_hash},
                    )
                )
                .mappings()
                .first()
            )
            if (
                audio is None
                or audio["status"] != "CONFIRMED"
                or audio["security_status"] != "PASSED"
                or not audio["object_key"]
                or not audio["duration_ms"]
            ):
                raise AppError("SLICE_SECURITY_REQUIRED", "切片须可解码且已通过安全审核", 409)
            await session.execute(
                text(
                    "UPDATE sentence_audio_slice SET status='CONFIRMED',"
                    "confirmed_by=:actor,confirmed_at=UTC_TIMESTAMP(6) WHERE public_id=:id"
                ),
                {"actor": actor, "id": audio["public_id"]},
            )
            await session.execute(
                text("UPDATE audio_version SET status='CONFIRMED' WHERE public_id=:id"),
                {"id": audio["resource_version"]},
            )
            sentence = next(item for item in content.dialogue if item.id == sentence_id)
            sentence.slice_target_id = audio["target_id"]
            sentence.slice_version = audio["resource_version"]
            sentence.slice_dependency_hash = dependency_hash
            return await self.persist(
                session,
                row,
                content,
                actor,
                "content.slice.confirm",
                {"sentence_id": sentence_id, "resource_version": audio["resource_version"]},
            )
