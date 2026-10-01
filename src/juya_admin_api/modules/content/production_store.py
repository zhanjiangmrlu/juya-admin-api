"""Transactional storage for complete V1.3 content and immutable dictionary versions."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.content.domain import PublishCheck, PublishedScene, SceneRevision
from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.text_spans import build_clickable_spans
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def decode(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ProductionStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        require_review: bool = True,
        prepare_asset: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.require_review = require_review
        self.prepare_asset = prepare_asset

    async def creation_replay(
        self, session: AsyncSession, scope: str, actor: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = (
            await session.execute(
                text(
                    "SELECT request_hash,response_body FROM idempotency_record "
                    "WHERE scope=:scope AND actor_id=:actor AND idempotency_key=:key FOR UPDATE"
                ),
                {"scope": scope, "actor": actor, "key": key},
            )
        ).first()
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同创建请求", 409)
        return dict(decode(row.response_body))

    async def remember_creation(
        self,
        session: AsyncSession,
        scope: str,
        actor: str,
        key: str,
        request_hash: str,
        result: dict[str, Any],
        now: datetime,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO idempotency_record(scope,actor_id,idempotency_key,request_hash,"
                "status,response_status,response_body,created_at,completed_at) "
                "VALUES(:scope,:actor,:key,:hash,'COMPLETED',201,:body,:now,:now)"
            ),
            {
                "scope": scope,
                "actor": actor,
                "key": key,
                "hash": request_hash,
                "body": encode(result),
                "now": now,
            },
        )

    async def template(self, session: AsyncSession, kind: str) -> int:
        await session.execute(
            text(
                "INSERT IGNORE INTO "
                "content_template(template_type,version,required_modules,validation_rules) "
                "VALUES (:kind,1,JSON_ARRAY('dialogue','vocabulary','chunks'),JSON_OBJECT())"
            ),
            {"kind": kind},
        )
        template = await session.scalar(
            text(
                "SELECT id FROM content_template WHERE template_type=:kind AND enabled=1 "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"kind": kind},
        )
        if template is None:
            raise AppError("TEMPLATE_DISABLED", "场景模板已停用", 409)
        return int(template)

    async def import_images(
        self, series_id: str, kind: str, asset_ids: list[str], actor: str, key: str
    ) -> list[str]:
        request_hash = hashlib.sha256(encode([series_id, kind, asset_ids]).encode()).hexdigest()
        now = datetime.now(UTC)
        async with self.sessions() as session, session.begin():
            series = await session.scalar(
                text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                {"id": series_id},
            )
            if series is None:
                raise AppError("SERIES_NOT_FOUND", "内容系列不存在", 404)
            replay = (
                await session.execute(
                    text(
                        "SELECT request_hash,response_body FROM idempotency_record WHERE "
                        "scope='content.import' "
                        "AND actor_id=:actor AND idempotency_key=:key FOR UPDATE"
                    ),
                    {"actor": actor, "key": key},
                )
            ).first()
            if replay:
                if replay.request_hash != request_hash:
                    raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同导入", 409)
                return list(decode(replay.response_body)["scene_ids"])
            template_id = await self.template(session, kind)
            scenes: list[str] = []
            for asset_id in dict.fromkeys(asset_ids):
                asset = (
                    await session.execute(
                        text(
                            "SELECT id,status,security_status,asset_type,width,height FROM "
                            "media_asset WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": asset_id},
                    )
                ).first()
                if (
                    not asset
                    or asset.status != "CONFIRMED"
                    or not security_status_usable(
                        asset.security_status, require_review=self.require_review
                    )
                    or asset.asset_type != "images"
                    or not asset.width
                    or not asset.height
                ):
                    raise AppError("IMAGE_NOT_READY", "原图尚未通过素材检查", 409)
                existing = await session.scalar(
                    text(
                        "SELECT s.public_id FROM scene s JOIN scene_revision r ON r.scene_id=s.id "
                        "WHERE s.series_id=:series AND s.template_id=:template "
                        "AND "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,"
                        "'$.original_image_asset_id'))=:asset "
                        "ORDER BY s.id LIMIT 1"
                    ),
                    {"series": series, "template": template_id, "asset": asset_id},
                )
                if existing:
                    scenes.append(str(existing))
                    continue
                scene_id, revision_id = new_ulid(now), new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO scene(public_id,series_id,template_id,title,status) "
                        "VALUES (:id,:series,:template,'','DRAFT')"
                    ),
                    {"id": scene_id, "series": series, "template": template_id},
                )
                internal_scene = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                content = SceneContent(original_image_asset_id=asset_id)
                await session.execute(
                    text(
                        "INSERT INTO "
                        "scene_revision(public_id,scene_id,version_no,edit_version,status,"
                        "content_snapshot,created_by,created_at) "
                        "VALUES (:id,:scene,1,1,'DRAFT',:content,:actor,:now)"
                    ),
                    {
                        "id": revision_id,
                        "scene": internal_scene,
                        "content": encode(content.model_dump()),
                        "actor": actor,
                        "now": now,
                    },
                )
                internal_revision = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                await session.execute(
                    text("UPDATE scene SET draft_revision_id=:revision WHERE id=:scene"),
                    {"revision": internal_revision, "scene": internal_scene},
                )
                await session.execute(
                    text("INSERT INTO scene_media_reference(revision_id,asset_id) VALUES (:r,:a)"),
                    {"r": internal_revision, "a": asset.id},
                )
                scenes.append(scene_id)
            await session.execute(
                text(
                    "INSERT INTO "
                    "idempotency_record(scope,actor_id,idempotency_key,request_hash,status,"
                    "response_status,response_body,created_at,completed_at) "
                    "VALUES ('content.import',:actor,:key,:hash,'COMPLETED',201,:body,:now,:now)"
                ),
                {
                    "actor": actor,
                    "key": key,
                    "hash": request_hash,
                    "body": encode({"scene_ids": scenes}),
                    "now": now,
                },
            )
            return scenes

    async def list_series(self) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.execute(
                text(
                    "SELECT public_id AS id,title,slug,cover_asset_id FROM "
                    "content_series ORDER BY sort_order,id"
                )
            )
            return [dict(row._mapping) for row in rows]

    async def create_series(
        self,
        title: str,
        slug: str,
        cover_asset_id: str | None,
        *,
        actor: str = "",
        key: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise AppError("SERIES_TITLE_REQUIRED", "系列名称不能为空", 422)
        now = datetime.now(UTC)
        public_id = new_ulid(now)
        request_hash = hashlib.sha256(encode([title, slug, cover_asset_id]).encode()).hexdigest()
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("SELECT id FROM discovery_config_state WHERE id=1 FOR UPDATE")
            )
            if key:
                replay = await self.creation_replay(
                    session, "content.series", actor, key, request_hash
                )
                if replay is not None:
                    return replay
            if await session.scalar(
                text("SELECT id FROM content_series WHERE slug=:slug"), {"slug": slug}
            ):
                raise AppError("SERIES_SLUG_CONFLICT", "系列标识已存在", 409)
            await session.execute(
                text(
                    "INSERT INTO content_series(public_id,slug,title,status,cover_asset_id) "
                    "VALUES (:id,:slug,:title,'DRAFT',:cover)"
                ),
                {"id": public_id, "slug": slug, "title": title, "cover": cover_asset_id},
            )
            result = {
                "id": public_id,
                "title": title,
                "slug": slug,
                "cover_asset_id": cover_asset_id,
            }
            if key:
                await self.remember_creation(
                    session, "content.series", actor, key, request_hash, result, now
                )
        return result

    async def create_scene(
        self, series_id: str, template_type: str, *, actor: str = "", key: str | None = None
    ) -> str:
        now = datetime.now(UTC)
        public_id = new_ulid(now)
        request_hash = hashlib.sha256(encode([series_id, template_type]).encode()).hexdigest()
        async with self.sessions() as session, session.begin():
            series = await session.scalar(
                text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                {"id": series_id},
            )
            if series is None:
                raise AppError("SERIES_NOT_FOUND", "内容系列不存在", 404)
            if key:
                replay = await self.creation_replay(
                    session, "content.scene", actor, key, request_hash
                )
                if replay is not None:
                    return str(replay["scene_id"])
            template = await self.template(session, template_type)
            await session.execute(
                text(
                    "INSERT INTO scene(public_id,series_id,template_id,title,status) "
                    "VALUES (:id,:series,:template,'','DRAFT')"
                ),
                {"id": public_id, "series": series, "template": template},
            )
            if key:
                internal_scene = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                revision_id = new_ulid(now)
                await session.execute(
                    text(
                        "INSERT INTO scene_revision(public_id,scene_id,version_no,edit_version,"
                        "status,content_snapshot,created_by,created_at) "
                        "VALUES(:id,:scene,1,1,'DRAFT',:content,:actor,:now)"
                    ),
                    {
                        "id": revision_id,
                        "scene": internal_scene,
                        "content": encode(SceneContent().model_dump()),
                        "actor": actor,
                        "now": now,
                    },
                )
                internal_revision = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                await session.execute(
                    text("UPDATE scene SET draft_revision_id=:revision WHERE id=:scene"),
                    {"revision": internal_revision, "scene": internal_scene},
                )
                await self.remember_creation(
                    session, "content.scene", actor, key, request_hash, {"scene_id": public_id}, now
                )
        return public_id

    async def list_lexicon(self, query: str = "") -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = await session.execute(
                text(
                    "SELECT e.entry_type,v.content FROM lexicon_entry e JOIN "
                    "lexicon_entry_version v "
                    "ON v.entry_id=e.public_id AND v.version=e.current_version "
                    "WHERE e.normalized_english LIKE :q ORDER BY e.normalized_english LIMIT 100"
                ),
                {"q": f"%{query.strip().casefold()}%"},
            )
            return [{**decode(row.content), "entry_type": row.entry_type} for row in rows]

    async def write_entry(self, entry: SceneEntry, kind: str, actor: str) -> SceneEntry:
        async with self.sessions() as session, session.begin():
            return await self._entry(session, entry, kind, actor)

    async def _entry(
        self, session: AsyncSession, entry: SceneEntry, kind: str, actor: str
    ) -> SceneEntry:
        now = datetime.now(UTC)
        normalized = " ".join(entry.english.split()).casefold()
        if not normalized:
            return entry
        row = (
            await session.execute(
                text(
                    "SELECT public_id,current_version,entry_type FROM lexicon_entry "
                    "WHERE public_id=:id OR (entry_type=:kind AND "
                    "normalized_english=:english) FOR UPDATE"
                ),
                {"id": entry.entry_id, "kind": kind, "english": normalized},
            )
        ).first()
        if row is None and entry.entry_id:
            raise AppError("ENTRY_NOT_FOUND", "词库条目不存在", 404)
        if row is None:
            entry = entry.model_copy(update={"entry_id": new_ulid(now), "entry_version": 1})
            await session.execute(
                text(
                    "INSERT INTO "
                    "lexicon_entry(public_id,entry_type,normalized_english,current_version,"
                    "created_by,created_at) VALUES (:id,:kind,:english,1,:actor,:now)"
                ),
                {
                    "id": entry.entry_id,
                    "kind": kind,
                    "english": normalized,
                    "actor": actor,
                    "now": now,
                },
            )
        else:
            if row.entry_type != kind or (entry.entry_id and row.public_id != entry.entry_id):
                raise AppError("ENTRY_IDENTITY_CONFLICT", "词条类型或标准词形冲突", 409)
            version = entry.entry_version if entry.entry_id else int(row.current_version)
            previous = await session.scalar(
                text("SELECT content FROM lexicon_entry_version WHERE entry_id=:id AND version=:v"),
                {"id": row.public_id, "v": version},
            )
            if previous is None:
                raise AppError("ENTRY_VERSION_NOT_FOUND", "词条版本不存在", 409)
            proposed = entry.model_copy(
                update={"entry_id": row.public_id, "entry_version": version}
            )
            old = SceneEntry.model_validate(decode(previous))
            # Source membership belongs to the scene, not the reusable dictionary definition.
            excluded = {"source_sentence_ids"}
            if proposed.model_dump(exclude=excluded) == old.model_dump(exclude=excluded):
                return proposed
            if entry.entry_id and version != int(row.current_version):
                raise AppError("ENTRY_VERSION_CONFLICT", "词库已更新; 请重新核对后编辑", 409)
            entry = proposed.model_copy(update={"entry_version": int(row.current_version) + 1})
            await session.execute(
                text(
                    "UPDATE lexicon_entry SET "
                    "current_version=:v,normalized_english=:english WHERE public_id=:id"
                ),
                {"v": entry.entry_version, "english": normalized, "id": entry.entry_id},
            )
        dictionary = entry.model_copy(update={"source_sentence_ids": []})
        await session.execute(
            text(
                "INSERT INTO lexicon_entry_version(entry_id,version,content,created_by,created_at) "
                "VALUES (:id,:v,:content,:actor,:now)"
            ),
            {
                "id": entry.entry_id,
                "v": entry.entry_version,
                "content": encode(dictionary.model_dump()),
                "actor": actor,
                "now": now,
            },
        )
        return entry

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None
    ) -> SceneRevision:
        async with self.sessions() as session, session.begin():
            scene = (
                await session.execute(
                    text("SELECT id FROM scene WHERE public_id=:id FOR UPDATE"),
                    {"id": revision.scene_id},
                )
            ).first()
            if scene is None:
                raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
            existing = (
                await session.execute(
                    text(
                        "SELECT id,edit_version,status FROM scene_revision WHERE "
                        "public_id=:id FOR UPDATE"
                    ),
                    {"id": revision.id},
                )
            ).first()
            if existing is not None:
                if existing.status in {"PUBLISHED", "SUPERSEDED"}:
                    raise AppError("PUBLISHED_REVISION_IMMUTABLE", "已发布版本不可原地修改", 409)
                if expected_version is not None and existing.edit_version != expected_version:
                    raise AppError(
                        "REVISION_VERSION_CONFLICT",
                        "内容草稿已更新",
                        409,
                        {
                            "current_revision_id": revision.id,
                            "current_version": existing.edit_version,
                        },
                    )
            elif expected_version is not None:
                raise AppError("REVISION_NOT_FOUND", "草稿不存在", 404)
            content = SceneContent.model_validate(revision.content)
            if not content.cover_asset_id:
                content.cover_asset_id = await session.scalar(
                    text(
                        "SELECT m.public_id FROM scene s JOIN content_series cs "
                        "ON cs.id=s.series_id JOIN media_asset m ON m.public_id=cs.cover_asset_id "
                        "WHERE s.id=:scene"
                    ),
                    {"scene": scene.id},
                )
            content.vocabulary = [
                await self._entry(session, entry, "VOCABULARY", revision.created_by)
                for entry in content.vocabulary
            ]
            content.chunks = [
                await self._entry(session, entry, "PHRASE", revision.created_by)
                for entry in content.chunks
            ]
            content = build_clickable_spans(content)
            revision.content = content.model_dump()
            revision.stable_sentence_ids = tuple(row.id for row in content.dialogue)
            revision.stable_entry_ids = tuple(
                entry.entry_id for entry in [*content.vocabulary, *content.chunks]
            )
            if existing is None:
                source = (
                    await session.scalar(
                        text(
                            "SELECT id FROM scene_revision WHERE public_id=:id AND scene_id=:scene"
                        ),
                        {"id": revision.source_revision_id, "scene": scene.id},
                    )
                    if revision.source_revision_id
                    else None
                )
                number = await session.scalar(
                    text(
                        "SELECT COALESCE(MAX(version_no),0)+1 FROM scene_revision "
                        "WHERE scene_id=:id"
                    ),
                    {"id": scene.id},
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "scene_revision(public_id,scene_id,source_revision_id,version_no,"
                        "edit_version,"
                        "status,content_snapshot,created_by,created_at) "
                        "VALUES (:id,:scene,:source,:number,1,'DRAFT',:content,:actor,:now)"
                    ),
                    {
                        "id": revision.id,
                        "scene": scene.id,
                        "source": source,
                        "number": number,
                        "content": encode(revision.content),
                        "actor": revision.created_by,
                        "now": revision.created_at or datetime.now(UTC),
                    },
                )
                internal_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
                revision.version = 1
            else:
                internal_id = existing.id
                revision.version = int(existing.edit_version) + 1
                await session.execute(
                    text(
                        "UPDATE scene_revision SET "
                        "edit_version=:v,content_snapshot=:content,status='DRAFT' WHERE id=:id"
                    ),
                    {"v": revision.version, "content": encode(revision.content), "id": internal_id},
                )
            await session.execute(
                text("UPDATE scene SET draft_revision_id=:revision WHERE id=:id"),
                {"revision": internal_id, "id": scene.id},
            )
            await self._sync_projection(session, int(internal_id), content)
            return revision

    async def _sync_projection(
        self, session: AsyncSession, revision: int, content: SceneContent
    ) -> None:
        for table in ("scene_entry", "scene_dialogue_sentence", "scene_media_reference"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE revision_id=:r"), {"r": revision}
            )
        for index, sentence in enumerate(content.dialogue):
            await session.execute(
                text(
                    "INSERT INTO "
                    "scene_dialogue_sentence(revision_id,stable_id,speaker,english_text,"
                    "chinese_text,sort_order) "
                    "VALUES (:r,:id,:speaker,:english,:chinese,:sort)"
                ),
                {
                    "r": revision,
                    "id": sentence.id,
                    "speaker": sentence.speaker,
                    "english": sentence.english,
                    "chinese": sentence.chinese,
                    "sort": index,
                },
            )
        for index, (entry, kind) in enumerate(
            [(entry, "VOCABULARY") for entry in content.vocabulary]
            + [(entry, "PHRASE") for entry in content.chunks]
        ):
            if not entry.entry_id:
                continue
            await session.execute(
                text(
                    "INSERT INTO "
                    "scene_entry(revision_id,stable_id,entry_type,normalized_english,phonetic,"
                    "chinese_text,explanation,sort_order) VALUES "
                    "(:r,:id,:kind,:english,:phonetic,:chinese,:explanation,:sort)"
                ),
                {
                    "r": revision,
                    "id": entry.entry_id,
                    "kind": kind,
                    "english": entry.english,
                    "phonetic": entry.phonetic,
                    "chinese": entry.chinese,
                    "explanation": entry.explanation,
                    "sort": index,
                },
            )
            entry_internal_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            sources: dict[str, list[str]] = {}
            for sentence in content.dialogue:
                for span in sentence.clickable_spans:
                    if span.entry_id == entry.entry_id:
                        sources.setdefault(span.source_locator, []).append(sentence.id)
            for order, (locator, ids) in enumerate(sources.items()):
                snapshot = "\n".join(row.english for row in content.dialogue if row.id in ids)
                await session.execute(
                    text(
                        "INSERT INTO "
                        "scene_entry_source(entry_id,source_stable_id,sentence_stable_id,"
                        "source_sentence_snapshot,sort_order) VALUES "
                        "(:entry,:source,:sentence,:snapshot,:order)"
                    ),
                    {
                        "entry": entry_internal_id,
                        "source": hashlib.sha256(locator.encode()).hexdigest()[:26],
                        "sentence": ids[0],
                        "snapshot": snapshot,
                        "order": order,
                    },
                )
        assets, _audio = await self.facts(session, content)
        for asset_id in assets:
            await session.execute(
                text(
                    "INSERT IGNORE INTO scene_media_reference(revision_id,asset_id) "
                    "SELECT :r,id FROM media_asset WHERE public_id=:id"
                ),
                {"r": revision, "id": asset_id},
            )

    async def facts(
        self, session: AsyncSession, content: SceneContent, *, lock: bool = False
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        asset_ids = {
            value for value in (content.original_image_asset_id, content.cover_asset_id) if value
        }
        versions = {
            entry.audio_version_id
            for entry in [*content.vocabulary, *content.chunks]
            if entry.audio_version_id
        }
        asset_ids.update(
            entry.icon_asset_id
            for entry in [*content.vocabulary, *content.chunks]
            if entry.icon_asset_id
        )
        if content.audio:
            asset_ids.add(content.audio.asset_id)
            versions.add(content.audio.version_id)
        audios: dict[str, dict[str, Any]] = {}
        for version in sorted(versions):
            row = (
                await session.execute(
                    text(
                        "SELECT v.public_id,v.status,t.public_id AS "
                        "target_id,m.public_id AS asset_id "
                        "FROM audio_version v JOIN audio_target t ON t.id=v.target_id "
                        "JOIN media_asset m ON m.id=v.asset_id WHERE v.public_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": version},
                )
            ).first()
            if row:
                audios[version] = dict(row._mapping)
                asset_ids.add(row.asset_id)
        assets: dict[str, dict[str, Any]] = {}
        for asset_id in sorted(asset_ids):
            row = (
                await session.execute(
                    text(
                        "SELECT "
                        "public_id,status,security_status,asset_type,duration_ms,"
                        "width,height,object_key "
                        "FROM media_asset WHERE public_id=:id" + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": asset_id},
                )
            ).first()
            if row:
                assets[asset_id] = dict(row._mapping)
        return assets, audios

    async def entry_references(self, session: AsyncSession, content: SceneContent) -> PublishCheck:
        valid = True
        for entry in [*content.vocabulary, *content.chunks]:
            snapshot = await session.scalar(
                text(
                    "SELECT content FROM lexicon_entry_version "
                    "WHERE entry_id=:id AND version=:version"
                ),
                {"id": entry.entry_id, "version": entry.entry_version},
            )
            if snapshot is None:
                valid = False
                break
            expected = SceneEntry.model_validate(decode(snapshot)).model_dump()
            actual = entry.model_dump()
            expected.pop("source_sentence_ids")
            actual.pop("source_sentence_ids")
            if expected != actual:
                valid = False
                break
        return PublishCheck("ENTRY_REFERENCE_INVALID", "ERROR", valid)

    async def prepare_resources(self, revision_id: str) -> None:
        """Fix legacy bytes before entering publication/receipt row-lock transactions."""
        if self.prepare_asset is None:
            return
        async with self.sessions() as session:
            snapshot = await session.scalar(
                text("SELECT content_snapshot FROM scene_revision WHERE public_id=:id"),
                {"id": revision_id},
            )
            if snapshot is None:
                raise AppError("REVISION_NOT_FOUND", "版本不存在", 404)
            assets, _ = await self.facts(session, SceneContent.model_validate(decode(snapshot)))
        for asset_id in sorted(assets):
            await self.prepare_asset(asset_id)

    async def checks(self, revision_id: str) -> list[PublishCheck]:
        await self.prepare_resources(revision_id)
        async with self.sessions() as session:
            snapshot = await session.scalar(
                text("SELECT content_snapshot FROM scene_revision WHERE public_id=:id"),
                {"id": revision_id},
            )
            if snapshot is None:
                raise AppError("REVISION_NOT_FOUND", "版本不存在", 404)
            content = SceneContent.model_validate(decode(snapshot))
            assets, audios = await self.facts(session, content)
            return [
                *check_content(content, assets, audios, require_review=self.require_review),
                await self.entry_references(session, content),
            ]

    async def publish(
        self, revision: SceneRevision, actor: str, key: str, now: datetime
    ) -> PublishedScene:
        await self.prepare_resources(revision.id)
        request_hash = hashlib.sha256(f"{revision.id}:{revision.version}".encode()).hexdigest()
        async with self.sessions() as session, session.begin():
            scene = (
                await session.execute(
                    text(
                        "SELECT id,published_revision_id FROM scene WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": revision.scene_id},
                )
            ).one()
            replay = (
                await session.execute(
                    text(
                        "SELECT request_hash,response_body FROM idempotency_record "
                        "WHERE scope='content.publish' AND actor_id=:actor AND "
                        "idempotency_key=:key FOR UPDATE"
                    ),
                    {"actor": actor, "key": key},
                )
            ).first()
            if replay:
                if replay.request_hash != request_hash:
                    raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他内容", 409)
                body = decode(replay.response_body)
                return PublishedScene(
                    body["scene_id"],
                    body["revision_id"],
                    datetime.fromisoformat(body["published_at"]),
                )
            row = (
                await session.execute(
                    text(
                        "SELECT id,edit_version,status,content_snapshot FROM "
                        "scene_revision WHERE public_id=:id FOR UPDATE"
                    ),
                    {"id": revision.id},
                )
            ).one()
            if row.edit_version != revision.version or row.status not in {
                "DRAFT",
                "REVIEWED",
                "PUBLISH_READY",
            }:
                raise AppError(
                    "REVISION_VERSION_CONFLICT",
                    "待发布草稿已变化",
                    409,
                    {"current_version": row.edit_version},
                )
            content = SceneContent.model_validate(decode(row.content_snapshot))
            assets, audios = await self.facts(session, content, lock=True)
            checks = [
                *check_content(content, assets, audios, require_review=self.require_review),
                await self.entry_references(session, content),
            ]
            errors = [check.code for check in checks if not check.passed]
            if errors:
                raise AppError(
                    "PUBLISH_CHECK_FAILED", "内容未通过发布检查", 409, {"error_codes": errors}
                )
            if scene.published_revision_id and scene.published_revision_id != row.id:
                await session.execute(
                    text("UPDATE scene_revision SET status='SUPERSEDED' WHERE id=:id"),
                    {"id": scene.published_revision_id},
                )
            await session.execute(
                text("UPDATE scene_revision SET status='PUBLISHED',published_at=:now WHERE id=:id"),
                {"id": row.id, "now": now},
            )
            cover = assets.get(content.cover_asset_id or "", {}).get("object_key")
            await session.execute(
                text(
                    "UPDATE scene SET "
                    "status='PUBLISHED',published_revision_id=:r,title=:title,summary=:summary,"
                    "cover_object_key=:cover,updated_at=:now WHERE id=:id"
                ),
                {
                    "id": scene.id,
                    "r": row.id,
                    "title": content.title_en,
                    "summary": content.summary,
                    "cover": cover,
                    "now": now,
                },
            )
            result = PublishedScene(revision.scene_id, revision.id, now)
            await session.execute(
                text(
                    "INSERT INTO "
                    "idempotency_record(scope,actor_id,idempotency_key,request_hash,status,"
                    "response_status,response_body,created_at,completed_at) "
                    "VALUES ('content.publish',:actor,:key,:hash,'COMPLETED',200,:body,:now,:now)"
                ),
                {
                    "actor": actor,
                    "key": key,
                    "hash": request_hash,
                    "now": now,
                    "body": encode(
                        {
                            "scene_id": result.scene_id,
                            "revision_id": result.revision_id,
                            "published_at": result.published_at.isoformat(),
                        }
                    ),
                },
            )
            return result

    async def full_scene(self, scene_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.public_id,r.version_no,r.content_snapshot FROM "
                        "scene s JOIN scene_revision r "
                        "ON r.id=s.published_revision_id WHERE s.public_id=:id AND "
                        "s.status='PUBLISHED'"
                    ),
                    {"id": scene_id},
                )
            ).first()
            if row is None:
                return None
            return {
                "scene_id": scene_id,
                "revision_id": row.public_id,
                "content_version": row.version_no,
                "content": decode(row.content_snapshot),
            }

    async def resource(
        self, scene_id: str, revision_id: str, resource_id: str, *, published: bool
    ) -> dict[str, Any]:
        async with self.sessions() as session:
            snapshot = await session.scalar(
                text(
                    "SELECT r.content_snapshot FROM scene_revision r JOIN scene s ON "
                    "s.id=r.scene_id "
                    "WHERE s.public_id=:scene AND r.public_id=:revision "
                    + (
                        "AND s.status='PUBLISHED' AND s.published_revision_id=r.id"
                        if published
                        else ""
                    )
                ),
                {"scene": scene_id, "revision": revision_id},
            )
            if snapshot is None:
                raise AppError("SCENE_VERSION_CONFLICT", "场景版本已变化; 请重新加载", 409)
            content = SceneContent.model_validate(decode(snapshot))
            assets, audios = await self.facts(session, content)
            version = audios.get(resource_id) or next(
                (row for row in audios.values() if row["target_id"] == resource_id), None
            )
            asset_id = version["asset_id"] if version else resource_id
            fact = assets.get(asset_id)
            if version and version["status"] not in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}:
                raise AppError("RESOURCE_NOT_READY", "音频版本尚未通过检查", 403)
            if (
                not fact
                or fact["status"] != "CONFIRMED"
                or not security_status_usable(
                    fact["security_status"], require_review=self.require_review
                )
            ):
                raise AppError("RESOURCE_NOT_REFERENCED", "该资源不属于当前场景版本", 403)
            return fact
