import hashlib
import json
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.domain import (
    OpenSceneConfig,
    PreviewConfig,
    PublishCheck,
    PublishedScene,
    Scene,
    SceneRevision,
)


class ContentRepository(Protocol):
    async def get_scene(self, scene_id: str) -> Scene | None: ...

    async def get_revision(self, revision_id: str) -> SceneRevision | None: ...

    async def save_revision(self, revision: SceneRevision) -> None: ...

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]: ...

    async def publish(
        self,
        revision: SceneRevision,
        actor_id: str,
        idempotency_key: str,
        published_at: datetime,
    ) -> PublishedScene: ...

    async def current_open_config(self) -> OpenSceneConfig | None: ...

    async def save_open_config(self, config: OpenSceneConfig) -> None: ...

    async def save_preview_config(self, config: PreviewConfig) -> None: ...

    async def save_scene(self, scene: Scene) -> None: ...


class InMemoryContentRepository:
    def __init__(self) -> None:
        self.scenes: dict[str, Scene] = {}
        self.revisions: dict[str, SceneRevision] = {}
        self.publish_checks: dict[str, list[PublishCheck]] = {}
        self.open_config: OpenSceneConfig | None = None
        self.preview_configs: dict[str, PreviewConfig] = {}
        self._published_commands: dict[tuple[str, str], PublishedScene] = {}

    async def get_scene(self, scene_id: str) -> Scene | None:
        return self.scenes.get(scene_id)

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        return self.revisions.get(revision_id)

    async def save_revision(self, revision: SceneRevision) -> None:
        self.revisions[revision.id] = revision
        scene = self.scenes[revision.scene_id]
        if revision.status == "DRAFT":
            scene.draft_revision_id = revision.id

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
        return list(self.publish_checks.get(revision_id, []))

    async def publish(
        self,
        revision: SceneRevision,
        actor_id: str,
        idempotency_key: str,
        published_at: datetime,
    ) -> PublishedScene:
        identity = (actor_id, idempotency_key)
        existing = self._published_commands.get(identity)
        if existing is not None:
            return existing
        scene = self.scenes[revision.scene_id]
        previous_id = scene.published_revision_id
        if previous_id is not None and previous_id != revision.id:
            self.revisions[previous_id].status = "SUPERSEDED"
        revision.status = "PUBLISHED"
        scene.published_revision_id = revision.id
        scene.status = "PUBLISHED"
        result = PublishedScene(scene.id, revision.id, published_at)
        self._published_commands[identity] = result
        return result

    async def current_open_config(self) -> OpenSceneConfig | None:
        return self.open_config

    async def save_open_config(self, config: OpenSceneConfig) -> None:
        self.open_config = config

    async def save_preview_config(self, config: PreviewConfig) -> None:
        self.preview_configs[config.series_id] = config

    async def save_scene(self, scene: Scene) -> None:
        self.scenes[scene.id] = scene


class SQLAlchemyContentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_scene(self, scene_id: str) -> Scene | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, cs.public_id AS series_public_id, s.status, "
                        "draft.public_id AS draft_revision_id, "
                        "published.public_id AS published_revision_id "
                        "FROM scene s JOIN content_series cs ON cs.id = s.series_id "
                        "LEFT JOIN scene_revision draft ON draft.id = s.draft_revision_id "
                        "LEFT JOIN scene_revision published ON published.id = "
                        "s.published_revision_id "
                        "WHERE s.public_id = :scene_id"
                    ),
                    {"scene_id": scene_id},
                )
            ).first()
        if row is None:
            return None
        return Scene(
            id=row.public_id,
            series_id=row.series_public_id,
            status=row.status,
            draft_revision_id=row.draft_revision_id,
            published_revision_id=row.published_revision_id,
        )

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.public_id, s.public_id AS scene_public_id, "
                        "source.public_id AS source_public_id, r.status, r.content_snapshot, "
                        "r.created_by, r.created_at FROM scene_revision r "
                        "JOIN scene s ON s.id = r.scene_id "
                        "LEFT JOIN scene_revision source ON source.id = r.source_revision_id "
                        "WHERE r.public_id = :revision_id"
                    ),
                    {"revision_id": revision_id},
                )
            ).first()
            if row is None:
                return None
            sentences: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT ds.stable_id FROM scene_dialogue_sentence ds "
                            "JOIN scene_revision r ON r.id = ds.revision_id "
                            "WHERE r.public_id = :revision_id ORDER BY ds.sort_order"
                        ),
                        {"revision_id": revision_id},
                    )
                )
                .scalars()
                .all()
            )
            entries: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT e.stable_id FROM scene_entry e "
                            "JOIN scene_revision r ON r.id = e.revision_id "
                            "WHERE r.public_id = :revision_id ORDER BY e.sort_order"
                        ),
                        {"revision_id": revision_id},
                    )
                )
                .scalars()
                .all()
            )
        return SceneRevision(
            id=row.public_id,
            scene_id=row.scene_public_id,
            source_revision_id=row.source_public_id,
            status=row.status,
            stable_sentence_ids=tuple(sentences),
            stable_entry_ids=tuple(entries),
            content=_json_dict(row.content_snapshot),
            created_by=row.created_by,
            created_at=_utc(row.created_at),
        )

    async def save_revision(self, revision: SceneRevision) -> None:
        async with self._session_factory() as session, session.begin():
            existing_id = await session.scalar(
                text("SELECT id FROM scene_revision WHERE public_id = :revision_id FOR UPDATE"),
                {"revision_id": revision.id},
            )
            snapshot = json.dumps(revision.content, ensure_ascii=False, separators=(",", ":"))
            if existing_id is not None:
                await session.execute(
                    text(
                        "UPDATE scene_revision SET content_snapshot = :snapshot, status = :status "
                        "WHERE id = :revision_id"
                    ),
                    {
                        "snapshot": snapshot,
                        "status": revision.status,
                        "revision_id": existing_id,
                    },
                )
                return
            scene_internal_id = await session.scalar(
                text("SELECT id FROM scene WHERE public_id = :scene_id FOR UPDATE"),
                {"scene_id": revision.scene_id},
            )
            if scene_internal_id is None:
                raise ValueError("scene disappeared while saving revision")
            source_id = None
            if revision.source_revision_id is not None:
                source_id = await session.scalar(
                    text("SELECT id FROM scene_revision WHERE public_id = :source_id"),
                    {"source_id": revision.source_revision_id},
                )
            version_no = await session.scalar(
                text(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM scene_revision "
                    "WHERE scene_id = :scene_id"
                ),
                {"scene_id": scene_internal_id},
            )
            await session.execute(
                text(
                    "INSERT INTO scene_revision "
                    "(public_id, scene_id, source_revision_id, version_no, status, "
                    "content_snapshot, created_by, created_at) VALUES "
                    "(:public_id, :scene_id, :source_id, :version_no, :status, :snapshot, "
                    ":created_by, :created_at)"
                ),
                {
                    "public_id": revision.id,
                    "scene_id": scene_internal_id,
                    "source_id": source_id,
                    "version_no": version_no,
                    "status": revision.status,
                    "snapshot": snapshot,
                    "created_by": revision.created_by,
                    "created_at": revision.created_at,
                },
            )
            revision_internal_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text("UPDATE scene SET draft_revision_id = :revision_id WHERE id = :scene_id"),
                {"revision_id": revision_internal_id, "scene_id": scene_internal_id},
            )

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT p.rule_code, p.severity, p.passed FROM publish_check_result p "
                        "JOIN scene_revision r ON r.id = p.revision_id "
                        "WHERE r.public_id = :revision_id"
                    ),
                    {"revision_id": revision_id},
                )
            ).all()
        return [PublishCheck(row.rule_code, row.severity, bool(row.passed)) for row in rows]

    async def publish(
        self,
        revision: SceneRevision,
        actor_id: str,
        idempotency_key: str,
        published_at: datetime,
    ) -> PublishedScene:
        request_hash = hashlib.sha256(revision.id.encode()).hexdigest()
        async with self._session_factory() as session, session.begin():
            replay = (
                await session.execute(
                    text(
                        "SELECT request_hash, response_body FROM idempotency_record "
                        "WHERE scope = 'content.publish' AND actor_id = :actor "
                        "AND idempotency_key = :key FOR UPDATE"
                    ),
                    {"actor": actor_id, "key": idempotency_key},
                )
            ).first()
            if replay is not None:
                if replay.request_hash != request_hash:
                    from juya_admin_api.shared.errors import AppError

                    raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
                body = _json_dict(replay.response_body)
                return PublishedScene(
                    str(body["scene_id"]),
                    str(body["revision_id"]),
                    datetime.fromisoformat(str(body["published_at"])),
                )
            row = (
                await session.execute(
                    text(
                        "SELECT r.id, r.scene_id, s.public_id AS scene_public_id, "
                        "s.published_revision_id FROM scene_revision r "
                        "JOIN scene s ON s.id = r.scene_id "
                        "WHERE r.public_id = :revision_id FOR UPDATE"
                    ),
                    {"revision_id": revision.id},
                )
            ).one()
            if row.published_revision_id is not None and row.published_revision_id != row.id:
                await session.execute(
                    text("UPDATE scene_revision SET status = 'SUPERSEDED' WHERE id = :id"),
                    {"id": row.published_revision_id},
                )
            await session.execute(
                text(
                    "UPDATE scene_revision SET status = 'PUBLISHED', published_at = :now "
                    "WHERE id = :revision_id"
                ),
                {"now": published_at, "revision_id": row.id},
            )
            await session.execute(
                text(
                    "UPDATE scene SET status = 'PUBLISHED', published_revision_id = :revision_id, "
                    "updated_at = :now WHERE id = :scene_id"
                ),
                {"revision_id": row.id, "now": published_at, "scene_id": row.scene_id},
            )
            result = PublishedScene(row.scene_public_id, revision.id, published_at)
            response_body = json.dumps(
                {
                    "scene_id": result.scene_id,
                    "revision_id": result.revision_id,
                    "published_at": result.published_at.isoformat(),
                },
                separators=(",", ":"),
            )
            await session.execute(
                text(
                    "INSERT INTO idempotency_record "
                    "(scope, actor_id, idempotency_key, request_hash, status, response_status, "
                    "response_body, created_at, completed_at) VALUES "
                    "('content.publish', :actor, :key, :request_hash, 'COMPLETED', 200, "
                    ":response_body, :now, :now)"
                ),
                {
                    "actor": actor_id,
                    "key": idempotency_key,
                    "request_hash": request_hash,
                    "response_body": response_body,
                    "now": published_at,
                },
            )
            return result

    async def current_open_config(self) -> OpenSceneConfig | None:
        async with self._session_factory() as session:
            config = (
                await session.execute(
                    text(
                        "SELECT id, version, activated_at, actor_public_id "
                        "FROM open_scene_config ORDER BY version DESC LIMIT 1"
                    )
                )
            ).first()
            if config is None:
                return None
            scene_ids: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id FROM open_scene_item i "
                            "JOIN scene s ON s.id = i.scene_id WHERE i.config_id = :config_id "
                            "ORDER BY i.position"
                        ),
                        {"config_id": config.id},
                    )
                )
                .scalars()
                .all()
            )
        if len(scene_ids) != 3:
            return None
        return OpenSceneConfig(
            config.version,
            (scene_ids[0], scene_ids[1], scene_ids[2]),
            _utc(config.activated_at),
            config.actor_public_id,
        )

    async def save_open_config(self, config: OpenSceneConfig) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO open_scene_config "
                    "(version, activated_at, actor_public_id, created_at) "
                    "VALUES (:version, :activated_at, :actor, :activated_at)"
                ),
                {
                    "version": config.version,
                    "activated_at": config.activated_at,
                    "actor": config.actor_id,
                },
            )
            config_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            for position, scene_id in enumerate(config.scene_ids, start=1):
                scene_internal_id = await session.scalar(
                    text("SELECT id FROM scene WHERE public_id = :scene_id"),
                    {"scene_id": scene_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO open_scene_item (config_id, scene_id, position) "
                        "VALUES (:config_id, :scene_id, :position)"
                    ),
                    {
                        "config_id": config_id,
                        "scene_id": scene_internal_id,
                        "position": position,
                    },
                )

    async def save_preview_config(self, config: PreviewConfig) -> None:
        async with self._session_factory() as session, session.begin():
            series_id = await session.scalar(
                text("SELECT id FROM content_series WHERE public_id = :series_id"),
                {"series_id": config.series_id},
            )
            await session.execute(
                text("DELETE FROM preview_config WHERE series_id = :series_id"),
                {"series_id": series_id},
            )
            for position, scene_id in enumerate(config.scene_ids, start=1):
                scene_internal_id = await session.scalar(
                    text("SELECT id FROM scene WHERE public_id = :scene_id"),
                    {"scene_id": scene_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO preview_config "
                        "(series_id, scene_id, position, enabled, updated_by, created_at) "
                        "VALUES (:series_id, :scene_id, :position, 1, :actor, :now)"
                    ),
                    {
                        "series_id": series_id,
                        "scene_id": scene_internal_id,
                        "position": position,
                        "actor": config.actor_id,
                        "now": config.updated_at,
                    },
                )

    async def save_scene(self, scene: Scene) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE scene SET status = :status WHERE public_id = :scene_id"),
                {"status": scene.status, "scene_id": scene.id},
            )

    async def is_open(self, scene_id: str, now: datetime) -> bool:
        del now
        async with self._session_factory() as session:
            value = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM open_scene_item i "
                    "JOIN open_scene_config c ON c.id = i.config_id "
                    "JOIN scene s ON s.id = i.scene_id WHERE s.public_id = :scene_id "
                    "AND c.version = (SELECT MAX(version) FROM open_scene_config)"
                ),
                {"scene_id": scene_id},
            )
        return bool(value)

    async def is_preview(self, scene_id: str, now: datetime) -> bool:
        del now
        async with self._session_factory() as session:
            value = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM preview_config p JOIN scene s ON s.id = p.scene_id "
                    "WHERE s.public_id = :scene_id AND p.enabled = 1"
                ),
                {"scene_id": scene_id},
            )
        return bool(value)

    async def list_learning_modules(self) -> list[dict[str, object]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT module_type, display_name, sort_order, entry_path "
                        "FROM learning_module_config WHERE enabled = 1 ORDER BY sort_order"
                    )
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    async def learning_catalog(self, user_id: str) -> list[dict[str, object]]:
        del user_id
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.public_id, s.title, s.summary, cs.title AS series, "
                        "s.cover_object_key FROM scene s JOIN content_series cs "
                        "ON cs.id = s.series_id WHERE s.status = 'PUBLISHED' "
                        "ORDER BY cs.sort_order, s.id"
                    )
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    async def get_full_scene(self, scene_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.content_snapshot FROM scene s JOIN scene_revision r "
                        "ON r.id = s.published_revision_id WHERE s.public_id = :scene_id "
                        "AND s.status = 'PUBLISHED'"
                    ),
                    {"scene_id": scene_id},
                )
            ).first()
        return None if row is None else _json_dict(row.content_snapshot)

    async def get_preview_scene(self, scene_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, s.title, cs.title AS series, s.cover_object_key, "
                        "p.introduction, 'PREVIEW' AS preview_status FROM preview_config p "
                        "JOIN scene s ON s.id = p.scene_id JOIN content_series cs "
                        "ON cs.id = s.series_id WHERE s.public_id = :scene_id AND p.enabled = 1"
                    ),
                    {"scene_id": scene_id},
                )
            ).first()
        return None if row is None else dict(row._mapping)

    async def get_entry(self, scene_id: str, entry_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT e.stable_id, e.entry_type, e.normalized_english, e.phonetic, "
                        "e.chinese_text, e.explanation FROM scene_entry e "
                        "JOIN scene_revision r ON r.id = e.revision_id JOIN scene s "
                        "ON s.published_revision_id = r.id WHERE s.public_id = :scene_id "
                        "AND e.stable_id = :entry_id"
                    ),
                    {"scene_id": scene_id, "entry_id": entry_id},
                )
            ).first()
        return None if row is None else dict(row._mapping)


def _json_dict(value: object) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return dict(decoded) if isinstance(decoded, dict) else {}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
