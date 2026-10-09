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
    # 功能:将数据库 JSON 字符串或映射转换为内容字典。
    # 参数:
    #     value: 数据库 JSON 字段的原始字符串或已解码结构,转换为空值安全的字典。
    # 返回:解析或复制得到的内容字典,空值对应空字典。
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def encode(value: object) -> str:
    # 功能:将业务对象序列化为保留中文字符的紧凑 JSON。
    # 参数:
    #     value: 需要存储为 JSON 的内容快照、请求字段或操作结果。
    # 返回:保留中文且不包含多余空白的 JSON 字符串。
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ProductionStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        require_review: bool = True,
        prepare_asset: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     sessions: 异步数据库会话工厂,为事务内的内容或额度读写提供会话。
        #     require_review: 是否要求素材通过内容审核;关闭时仍保留素材完整性检查。
        #     prepare_asset: 按素材公开标识准备不可变对象的异步回调,可为空。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.sessions = sessions
        self.require_review = require_review
        self.prepare_asset = prepare_asset

    async def creation_replay(
        self, session: AsyncSession, scope: str, actor: str, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        # 功能:锁定幂等创建记录,核对请求摘要并读取已保存结果。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     scope: 幂等记录的业务操作范围,隔离不同创建流程。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        #     request_hash: 创建请求内容的 SHA-256 摘要,识别幂等键是否被不同请求复用。
        # 返回:已完成创建请求的结果字典;没有幂等记录时为 None。
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
        # 功能:在当前事务内保存创建请求摘要和响应以支持重放。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     scope: 幂等记录的业务操作范围,隔离不同创建流程。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        #     request_hash: 创建请求内容的 SHA-256 摘要,识别幂等键是否被不同请求复用。
        #     result: 本次创建或批任务项执行结果字典,写入幂等回执或审计摘要。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:初始化默认模板并读取指定类型的最新启用模板。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     kind: 场景模板类型,区分 dialogue 对话与 vocabulary 词汇模板。
        # 返回:数据库中最新启用模板的内部整数主键。
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
    ) -> dict[str, list[str]]:
        # 功能:校验已确认原图并按系列和模板创建或复用场景草稿。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     kind: 场景模板类型,区分 dialogue 对话与 vocabulary 词汇模板。
        #     asset_ids: 待导入原图的素材公开标识列表,重复素材会去重或复用场景。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        # 返回:全部场景及复用场景的公开标识列表,供上传页面区分新建和复用
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
                saved = decode(replay.response_body)
                return {
                    "scene_ids": list(saved["scene_ids"]),
                    "reused_scene_ids": list(saved.get("reused_scene_ids", saved["scene_ids"])),
                }
            template_id = await self.template(session, kind)
            scenes: list[str] = []
            reused: list[str] = []
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
                    reused.append(str(existing))
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
                    "body": encode({"scene_ids": scenes, "reused_scene_ids": reused}),
                    "now": now,
                },
            )
            return {"scene_ids": scenes, "reused_scene_ids": reused}

    async def list_series(self) -> list[dict[str, Any]]:
        # 功能:读取内容系列及展示排序信息。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        # 返回:匹配的业务记录列表。
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
        # 功能:按幂等创建请求新增内容系列和封面引用。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     title: 内容系列展示标题,创建前按请求约束去除首尾空白。
        #     slug: 内容系列的可读短标识,由小写字母、数字和连字符组成。
        #     cover_asset_id: 系列封面的图片素材标识,可为空。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        # 返回:已创建或由幂等记录重放的系列字段。
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
        # 功能:为内容系列创建指定模板的空场景和初始草稿。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     template_type: 内容模板类型,区分 dialogue 对话与 vocabulary 词汇。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        # 返回:新建场景的公开标识。
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
        # 功能:按英文检索条件读取词汇和语块词典。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        # 返回:匹配的业务记录列表。
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
        # 功能:在事务中保存词典条目及不可变的内容版本。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     entry: 词典条目内容,含英文、中文、音标及指定词典版本引用。
        #     kind: 词典条目类别,VOCABULARY 为词汇,PHRASE 为语块。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        # 返回:已保存的词典条目及其版本引用。
        async with self.sessions() as session, session.begin():
            return await self._entry(session, entry, kind, actor)

    async def _entry(
        self, session: AsyncSession, entry: SceneEntry, kind: str, actor: str
    ) -> SceneEntry:
        # 功能:校验词典条目类型并创建或更新条目及版本快照。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     entry: 词典条目内容,含英文、中文、音标及指定词典版本引用。
        #     kind: 词典条目类别,VOCABULARY 为词汇,PHRASE 为语块。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        # 返回:已保存的词典条目及其版本引用。
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
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:内容版本对象及完整快照。
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
        # 功能:同步草稿的对话、词条、媒体引用等关系表投影。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     revision: 场景版本在数据库中的内部整数主键,关联关系表投影。
        #     content: 结构化场景内容,含标题、对话、词汇、语块和媒体引用。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:读取内容引用的素材及音频版本事实,按需加行锁。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     content: 结构化场景内容,含标题、对话、词汇、语块和媒体引用。
        #     lock: 是否对素材及音频查询加 FOR UPDATE 行锁,发布事务开启时使用。
        # 返回:素材事实字典与音频版本事实字典组成的二元组。
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
        # 功能:核对场景词条快照与其引用的不可变词典版本一致。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     session: 当前异步数据库会话,使读写共享外层事务。
        #     content: 结构化场景内容,含标题、对话、词汇、语块和媒体引用。
        # 返回:词典引用一致性检查项。
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
        # 功能:在发布行锁事务前准备草稿素材,修复遗留对象字节。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:准备草稿资源并汇总内容和词典引用的发布检查。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:词典引用一致性检查项的列表。
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
        # 功能:校验版本和内容后发布场景快照并保存幂等回执。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     actor: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     key: 当前业务操作的幂等键,防止重复创建或执行。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:已发布场景、版本标识和发布时间。
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
            # 未单独设置封面时,使用已通过发布校验的原始教材图片。
            cover = assets.get(
                content.cover_asset_id or content.original_image_asset_id or "", {}
            ).get("object_key")
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
        # 功能:读取当前已发布场景的完整内容快照。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:已发布场景标识、内容版本和完整快照;不存在时为 None。
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
        # 功能:核对资源属于指定场景版本且素材可用,读取资源对象信息。
        # 参数:
        #     self: 当前 ProductionStore 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     resource_id: 内容快照内的素材、音频目标或音频版本标识。
        #     published: 是否限定为场景当前已发布版本;草稿管理预览传入 False。
        # 返回:可用资源的素材事实,包含固定存储对象键。
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
