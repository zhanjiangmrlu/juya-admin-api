import json
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.domain import (
    AdminPreview,
    DiscoveryConfig,
    OpenSceneConfig,
    PreviewConfig,
    PublishCheck,
    PublishedScene,
    Scene,
    ScenePage,
    SceneRevision,
)
from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.shared.errors import AppError


class ContentRepository(Protocol):
    async def list_scenes(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None,
        series_id: str | None,
        status: str | None,
    ) -> ScenePage: ...

    async def get_scene(self, scene_id: str) -> Scene | None: ...

    async def get_revision(self, revision_id: str) -> SceneRevision | None: ...

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None = None
    ) -> SceneRevision: ...

    async def get_discovery_config(self) -> DiscoveryConfig: ...

    async def save_discovery_config(
        self, config: DiscoveryConfig, expected_version: int
    ) -> DiscoveryConfig: ...

    async def admin_preview(self, revision_id: str) -> AdminPreview | None: ...

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
        self.assets: dict[str, dict[str, object]] = {}
        self.audio_versions: dict[str, dict[str, object]] = {}
        self.open_config: OpenSceneConfig | None = None
        self.preview_configs: dict[str, PreviewConfig] = {}
        self.discovery_config = DiscoveryConfig(0, (), {}, {})
        self._published_commands: dict[tuple[str, str], PublishedScene] = {}

    async def list_scenes(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None,
        series_id: str | None,
        status: str | None,
    ) -> ScenePage:
        normalized_query = (query or "").strip().casefold()
        items = [
            scene
            for scene in self.scenes.values()
            if (series_id is None or scene.series_id == series_id)
            and (status is None or scene.status == status)
            and (
                not normalized_query
                or normalized_query in scene.id.casefold()
                or normalized_query in scene.title.casefold()
            )
        ]
        items.sort(key=lambda item: item.id)
        start = (page - 1) * page_size
        return ScenePage(tuple(items[start : start + page_size]), page, page_size, len(items))

    async def get_scene(self, scene_id: str) -> Scene | None:
        return self.scenes.get(scene_id)

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        return self.revisions.get(revision_id)

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None = None
    ) -> SceneRevision:
        current = self.revisions.get(revision.id)
        if current is not None and expected_version is not None:
            if current.version != expected_version:
                raise _revision_conflict(current)
            revision.version = current.version + 1
        self.revisions[revision.id] = revision
        scene = self.scenes[revision.scene_id]
        if revision.status == "DRAFT":
            scene.draft_revision_id = revision.id
        return revision

    async def get_discovery_config(self) -> DiscoveryConfig:
        return DiscoveryConfig(
            self.discovery_config.version,
            tuple(self.discovery_config.open_scene_ids),
            dict(self.discovery_config.preview_by_series),
            dict(self.discovery_config.learning_modules),
            self.discovery_config.updated_at,
            self.discovery_config.actor_id,
        )

    async def save_discovery_config(
        self, config: DiscoveryConfig, expected_version: int
    ) -> DiscoveryConfig:
        if self.discovery_config.version != expected_version:
            raise AppError(
                "DISCOVERY_CONFIG_VERSION_CONFLICT",
                "发现页配置已被其他管理员更新",
                409,
                {"current_version": self.discovery_config.version},
            )
        self.discovery_config = config
        self.open_config = OpenSceneConfig(
            config.version,
            (config.open_scene_ids[0], config.open_scene_ids[1], config.open_scene_ids[2]),
            config.updated_at or datetime.now(UTC),
            config.actor_id or "",
        )
        self.preview_configs = {
            series_id: PreviewConfig(
                series_id,
                scene_ids,
                config.updated_at or datetime.now(UTC),
                config.actor_id or "",
            )
            for series_id, scene_ids in config.preview_by_series.items()
        }
        return await self.get_discovery_config()

    async def admin_preview(self, revision_id: str) -> AdminPreview | None:
        revision = self.revisions.get(revision_id)
        if revision is None:
            return None
        scene = self.scenes.get(revision.scene_id)
        if scene is None:
            return None
        return AdminPreview(
            scene.id,
            revision.id,
            revision.status,
            scene.title,
            scene.series_title,
            dict(revision.content),
        )

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
        revision = self.revisions[revision_id]
        return check_content(
            SceneContent.model_validate(revision.content), self.assets, self.audio_versions
        )

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

    async def list_scenes(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None,
        series_id: str | None,
        status: str | None,
    ) -> ScenePage:
        params = {
            "query": None if query is None or not query.strip() else f"%{query.strip()}%",
            "series_id": series_id,
            "status": status,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        where = (
            "WHERE (:query IS NULL OR s.public_id LIKE :query OR s.title LIKE :query) "
            "AND (:series_id IS NULL OR cs.public_id = :series_id) "
            "AND (:status IS NULL OR s.status = :status) "
        )
        async with self._session_factory() as session:
            total = int(
                await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM scene s JOIN content_series cs "
                        "ON cs.id = s.series_id " + where
                    ),
                    params,
                )
                or 0
            )
            rows = (
                await session.execute(
                    text(
                        "SELECT s.public_id, cs.public_id AS series_public_id, "
                        "s.title, cs.title AS series_title, s.summary, s.cover_object_key, "
                        "s.status, draft.public_id AS draft_revision_id, "
                        "published.public_id AS published_revision_id, s.updated_at "
                        "FROM scene s JOIN content_series cs ON cs.id = s.series_id "
                        "LEFT JOIN scene_revision draft ON draft.id = s.draft_revision_id "
                        "LEFT JOIN scene_revision published ON published.id = "
                        "s.published_revision_id "
                        + where
                        + "ORDER BY s.public_id LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).all()
        items = tuple(_scene_from_row(row) for row in rows)
        return ScenePage(items, page, page_size, total)

    async def get_scene(self, scene_id: str) -> Scene | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, cs.public_id AS series_public_id, s.title, "
                        "cs.title AS series_title, s.summary, s.cover_object_key, s.status, "
                        "draft.public_id AS draft_revision_id, "
                        "published.public_id AS published_revision_id, s.updated_at "
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
        return _scene_from_row(row)

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.public_id, s.public_id AS scene_public_id, "
                        "source.public_id AS source_public_id, r.edit_version, r.status, "
                        "r.content_snapshot, "
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
            version=int(row.edit_version),
            status=row.status,
            stable_sentence_ids=tuple(sentences),
            stable_entry_ids=tuple(entries),
            content=_json_dict(row.content_snapshot),
            created_by=row.created_by,
            created_at=_utc(row.created_at),
        )

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None = None
    ) -> SceneRevision:
        return await ProductionStore(self._session_factory).save_revision(
            revision, expected_version
        )

    async def get_discovery_config(self) -> DiscoveryConfig:
        async with self._session_factory() as session, session.begin():
            state = (
                await session.execute(
                    text(
                        "SELECT version, updated_at, updated_by FROM discovery_config_state "
                        "WHERE id = 1"
                    )
                )
            ).one()
            open_rows: list[str] = list(
                (
                    await session.execute(
                        text(
                            "SELECT s.public_id FROM open_scene_item i "
                            "JOIN open_scene_config c ON c.id = i.config_id "
                            "JOIN scene s ON s.id = i.scene_id "
                            "WHERE c.version = (SELECT MAX(version) FROM open_scene_config) "
                            "ORDER BY i.position"
                        )
                    )
                )
                .scalars()
                .all()
            )
            preview_rows = (
                await session.execute(
                    text(
                        "SELECT cs.public_id AS series_id, s.public_id AS scene_id "
                        "FROM preview_config p JOIN content_series cs ON cs.id = p.series_id "
                        "JOIN scene s ON s.id = p.scene_id WHERE p.enabled = 1 "
                        "ORDER BY cs.public_id, p.position"
                    )
                )
            ).all()
            module_rows = (
                await session.execute(
                    text(
                        "SELECT module_type, enabled FROM learning_module_config "
                        "ORDER BY sort_order"
                    )
                )
            ).all()
        preview_by_series: dict[str, list[str]] = {}
        for row in preview_rows:
            preview_by_series.setdefault(row.series_id, []).append(row.scene_id)
        return DiscoveryConfig(
            int(state.version),
            tuple(open_rows),
            {key: tuple(value) for key, value in preview_by_series.items()},
            {row.module_type: bool(row.enabled) for row in module_rows},
            _utc(state.updated_at),
            state.updated_by,
        )

    async def save_discovery_config(
        self, config: DiscoveryConfig, expected_version: int
    ) -> DiscoveryConfig:
        async with self._session_factory() as session, session.begin():
            current_version = int(
                await session.scalar(
                    text("SELECT version FROM discovery_config_state WHERE id = 1 FOR UPDATE")
                )
                or 0
            )
            if current_version != expected_version:
                raise AppError(
                    "DISCOVERY_CONFIG_VERSION_CONFLICT",
                    "发现页配置已被其他管理员更新",
                    409,
                    {"current_version": current_version},
                )
            await session.execute(
                text(
                    "INSERT INTO open_scene_config "
                    "(version, activated_at, actor_public_id, created_at) "
                    "VALUES (:version, :now, :actor, :now)"
                ),
                {"version": config.version, "now": config.updated_at, "actor": config.actor_id},
            )
            config_id = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            for position, scene_id in enumerate(config.open_scene_ids, start=1):
                scene_internal_id = await session.scalar(
                    text("SELECT id FROM scene WHERE public_id = :scene_id"),
                    {"scene_id": scene_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO open_scene_item (config_id, scene_id, position) "
                        "VALUES (:config_id, :scene_id, :position)"
                    ),
                    {"config_id": config_id, "scene_id": scene_internal_id, "position": position},
                )
            await session.execute(text("DELETE FROM preview_config"))
            for series_public_id, scene_ids in config.preview_by_series.items():
                series_internal_id = await session.scalar(
                    text("SELECT id FROM content_series WHERE public_id = :series_id"),
                    {"series_id": series_public_id},
                )
                for position, scene_id in enumerate(scene_ids, start=1):
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
                            "series_id": series_internal_id,
                            "scene_id": scene_internal_id,
                            "position": position,
                            "actor": config.actor_id,
                            "now": config.updated_at,
                        },
                    )
            for module_type, enabled in config.learning_modules.items():
                await session.execute(
                    text(
                        "UPDATE learning_module_config SET enabled = :enabled "
                        "WHERE module_type = :module_type"
                    ),
                    {"enabled": enabled, "module_type": module_type},
                )
            await session.execute(
                text(
                    "UPDATE discovery_config_state SET version = :version, "
                    "updated_by = :actor, updated_at = :now WHERE id = 1"
                ),
                {"version": config.version, "actor": config.actor_id, "now": config.updated_at},
            )
        return config

    async def admin_preview(self, revision_id: str) -> AdminPreview | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id AS scene_id, r.public_id AS revision_id, "
                        "r.status AS revision_status, s.title AS scene_title, "
                        "cs.title AS series_title, r.content_snapshot "
                        "FROM scene_revision r JOIN scene s ON s.id = r.scene_id "
                        "JOIN content_series cs ON cs.id = s.series_id "
                        "WHERE r.public_id = :revision_id"
                    ),
                    {"revision_id": revision_id},
                )
            ).first()
        if row is None:
            return None
        return AdminPreview(
            row.scene_id,
            row.revision_id,
            row.revision_status,
            row.scene_title,
            row.series_title,
            _json_dict(row.content_snapshot),
        )

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
        return await ProductionStore(self._session_factory).checks(revision_id)

    async def publish(
        self, revision: SceneRevision, actor_id: str, idempotency_key: str, published_at: datetime
    ) -> PublishedScene:
        return await ProductionStore(self._session_factory).publish(
            revision, actor_id, idempotency_key, published_at
        )

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
        return await ProductionStore(self._session_factory).full_scene(scene_id)

    async def get_preview_scene(self, scene_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, s.title, cs.title AS series, s.cover_object_key, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_en')) AS title_en, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_zh')) AS title_zh, "
                        "p.introduction, 'PREVIEW' AS preview_status FROM preview_config p "
                        "JOIN scene s ON s.id = p.scene_id JOIN content_series cs "
                        "ON cs.id = s.series_id JOIN scene_revision r "
                        "ON r.id=s.published_revision_id WHERE s.public_id = :scene_id "
                        "AND p.enabled = 1 AND s.status='PUBLISHED'"
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


def _scene_from_row(row: object) -> Scene:
    return Scene(
        id=row.public_id,  # type: ignore[attr-defined]
        series_id=row.series_public_id,  # type: ignore[attr-defined]
        title=row.title,  # type: ignore[attr-defined]
        series_title=row.series_title,  # type: ignore[attr-defined]
        summary=row.summary,  # type: ignore[attr-defined]
        cover_object_key=row.cover_object_key,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        draft_revision_id=row.draft_revision_id,  # type: ignore[attr-defined]
        published_revision_id=row.published_revision_id,  # type: ignore[attr-defined]
        updated_at=_utc(row.updated_at),  # type: ignore[attr-defined]
    )


def _revision_conflict(revision: SceneRevision) -> AppError:
    return AppError(
        "REVISION_VERSION_CONFLICT",
        "内容草稿已被其他管理员更新",
        409,
        {"current_revision_id": revision.id, "current_version": revision.version},
    )
