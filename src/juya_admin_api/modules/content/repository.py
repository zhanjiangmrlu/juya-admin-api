import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.access_policy.entitlement_projection import limited_achievements
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
    ) -> ScenePage:
        # 功能:按系列、状态和检索条件分页读取场景。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     status: 待写入或筛选的业务状态代码。
        # 返回:场景分页记录和总数。
        ...

    async def list_revision_history(
        self, scene_id: str, page: int, page_size: int
    ) -> dict[str, object]:
        # 功能:分页读取场景的内容版本历史。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:内容版本列表及分页元信息。
        ...

    async def get_scene(self, scene_id: str) -> Scene | None:
        # 功能:按公开标识读取场景及其当前版本引用。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:场景对象及当前版本引用;未找到对应记录时为 None。
        ...

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        # 功能:按公开标识读取内容版本和编辑状态。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:内容版本对象及完整快照;未找到对应记录时为 None。
        ...

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None = None
    ) -> SceneRevision:
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:内容版本对象及完整快照。
        ...

    async def get_discovery_config(self) -> DiscoveryConfig:
        # 功能:读取开放场景、系列预览和学习模块配置。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:发现页配置及配置版本。
        ...

    async def save_discovery_config(
        self, config: DiscoveryConfig, expected_version: int
    ) -> DiscoveryConfig:
        # 功能:按预期配置版本保存开放场景、系列预览和学习模块开关。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景、系列预览和学习模块的发现页配置。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:发现页配置及配置版本。
        ...

    async def admin_preview(self, revision_id: str) -> AdminPreview | None:
        # 功能:读取内容版本的管理端预览快照。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:管理端预览对象和内容快照;未找到对应记录时为 None。
        ...

    async def list_publish_checks(self, revision_id: str) -> list[PublishCheck]:
        # 功能:汇总指定内容版本的发布检查项。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:词典引用一致性检查项的列表。
        ...

    async def publish(
        self,
        revision: SceneRevision,
        actor_id: str,
        idempotency_key: str,
        published_at: datetime,
    ) -> PublishedScene:
        # 功能:校验版本和内容后发布场景快照并保存幂等回执。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        #     published_at: 内容发布生效时间,同时写入发布记录和幂等响应。
        # 返回:已发布场景、版本标识和发布时间。
        ...

    async def current_open_config(self) -> OpenSceneConfig | None:
        # 功能:读取当前生效的开放场景配置。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:开放场景配置及生效时间;未找到对应记录时为 None。
        ...

    async def save_open_config(self, config: OpenSceneConfig) -> None:
        # 功能:保存当前开放场景和配置生效信息。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景标识及生效时间的配置对象。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def save_preview_config(self, config: PreviewConfig) -> None:
        # 功能:保存指定系列的预览场景配置。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 指定系列的预览场景及生效时间配置。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...

    async def save_scene(self, scene: Scene) -> None:
        # 功能:保存场景状态和当前内容版本引用。
        # 参数:
        #     self: 当前 ContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene: 场景领域对象,含系列和当前草稿、发布版本引用。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...


class InMemoryContentRepository:
    def __init__(self, *, require_review: bool = True) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     require_review: 是否要求素材通过内容审核;关闭时仍保留素材完整性检查。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._require_review = require_review
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
        # 功能:按系列、状态和检索条件分页读取场景。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     status: 待写入或筛选的业务状态代码。
        # 返回:场景分页记录和总数。
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
        # 匿名函数: 按场景公开标识构造确定性的场景排序键。
        # 参数:
        #     item: 待排序的场景领域对象。
        # 返回: 场景公开标识字符串。
        items.sort(key=lambda item: item.id)
        start = (page - 1) * page_size
        return ScenePage(tuple(items[start : start + page_size]), page, page_size, len(items))

    async def list_revision_history(
        self, scene_id: str, page: int, page_size: int
    ) -> dict[str, object]:
        # 功能:分页读取场景的内容版本历史。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:内容版本列表及分页元信息。
        revisions = [row for row in self.revisions.values() if row.scene_id == scene_id]
        items = [
            {
                "id": row.id,
                "version_no": number,
                "edit_version": row.version,
                "status": row.status,
                "source_revision_id": row.source_revision_id,
                "created_at": row.created_at,
                "created_by": row.created_by,
                "title_en": row.content.get("title_en", ""),
                "is_current": self.scenes[scene_id].published_revision_id == row.id,
            }
            for number, row in reversed(list(enumerate(revisions, 1)))
        ]
        start = (page - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": len(items),
        }

    async def get_scene(self, scene_id: str) -> Scene | None:
        # 功能:按公开标识读取场景及其当前版本引用。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:场景对象及当前版本引用;未找到对应记录时为 None。
        return self.scenes.get(scene_id)

    async def get_revision(self, revision_id: str) -> SceneRevision | None:
        # 功能:按公开标识读取内容版本和编辑状态。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:内容版本对象及完整快照;未找到对应记录时为 None。
        return self.revisions.get(revision_id)

    async def save_revision(
        self, revision: SceneRevision, expected_version: int | None = None
    ) -> SceneRevision:
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:内容版本对象及完整快照。
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
        # 功能:读取开放场景、系列预览和学习模块配置。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:发现页配置及配置版本。
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
        # 功能:按预期配置版本保存开放场景、系列预览和学习模块开关。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景、系列预览和学习模块的发现页配置。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:发现页配置及配置版本。
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
        # 功能:读取内容版本的管理端预览快照。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:管理端预览对象和内容快照;未找到对应记录时为 None。
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
        # 功能:汇总指定内容版本的发布检查项。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:词典引用一致性检查项的列表。
        revision = self.revisions[revision_id]
        return check_content(
            SceneContent.model_validate(revision.content),
            self.assets,
            self.audio_versions,
            require_review=self._require_review,
        )

    async def publish(
        self,
        revision: SceneRevision,
        actor_id: str,
        idempotency_key: str,
        published_at: datetime,
    ) -> PublishedScene:
        # 功能:校验版本和内容后发布场景快照并保存幂等回执。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        #     published_at: 内容发布生效时间,同时写入发布记录和幂等响应。
        # 返回:已发布场景、版本标识和发布时间。
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
        # 功能:读取当前生效的开放场景配置。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:开放场景配置及生效时间;未找到对应记录时为 None。
        return self.open_config

    async def save_open_config(self, config: OpenSceneConfig) -> None:
        # 功能:保存当前开放场景和配置生效信息。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景标识及生效时间的配置对象。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.open_config = config

    async def save_preview_config(self, config: PreviewConfig) -> None:
        # 功能:保存指定系列的预览场景配置。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 指定系列的预览场景及生效时间配置。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.preview_configs[config.series_id] = config

    async def save_scene(self, scene: Scene) -> None:
        # 功能:保存场景状态和当前内容版本引用。
        # 参数:
        #     self: 当前 InMemoryContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene: 场景领域对象,含系列和当前草稿、发布版本引用。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.scenes[scene.id] = scene


class SQLAlchemyContentRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        require_review: bool = True,
        prepare_asset: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     session_factory: 异步数据库会话工厂,为每次仓储操作提供会话。
        #     require_review: 是否要求素材通过内容审核;关闭时仍保留素材完整性检查。
        #     prepare_asset: 按素材公开标识准备不可变对象的异步回调,可为空。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._session_factory = session_factory
        self._require_review = require_review
        self._prepare_asset = prepare_asset

    async def list_scenes(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None,
        series_id: str | None,
        status: str | None,
    ) -> ScenePage:
        # 功能:按系列、状态和检索条件分页读取场景。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     status: 待写入或筛选的业务状态代码。
        # 返回:场景分页记录和总数。
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
                        "s.status, ct.template_type, draft.public_id AS draft_revision_id, "
                        "published.public_id AS published_revision_id, s.updated_at "
                        "FROM scene s JOIN content_series cs ON cs.id = s.series_id "
                        "LEFT JOIN content_template ct ON ct.id=s.template_id "
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

    async def list_revision_history(
        self, scene_id: str, page: int, page_size: int
    ) -> dict[str, object]:
        # 功能:分页读取场景的内容版本历史。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:内容版本列表及分页元信息。
        async with self._session_factory() as session:
            total = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM scene_revision r JOIN scene s ON s.id=r.scene_id "
                    "WHERE s.public_id=:scene"
                ),
                {"scene": scene_id},
            )
            rows = await session.execute(
                text(
                    "SELECT r.public_id AS id,r.version_no,r.edit_version,r.status,"
                    "source.public_id AS source_revision_id,r.created_at,r.created_by,"
                    "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_en')) AS title_en,"
                    "COALESCE((s.published_revision_id=r.id),0) AS is_current "
                    "FROM scene_revision r "
                    "JOIN scene s ON s.id=r.scene_id "
                    "LEFT JOIN scene_revision source ON source.id=r.source_revision_id "
                    "WHERE s.public_id=:scene ORDER BY r.version_no DESC,r.id DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"scene": scene_id, "limit": page_size, "offset": (page - 1) * page_size},
            )
            return {
                "items": [dict(row._mapping) for row in rows],
                "page": page,
                "page_size": page_size,
                "total": int(total or 0),
            }

    async def get_scene(self, scene_id: str) -> Scene | None:
        # 功能:按公开标识读取场景及其当前版本引用。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:场景对象及当前版本引用;未找到对应记录时为 None。
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, cs.public_id AS series_public_id, s.title, "
                        "cs.title AS series_title, s.summary, s.cover_object_key, s.status, "
                        "ct.template_type, "
                        "draft.public_id AS draft_revision_id, "
                        "published.public_id AS published_revision_id, s.updated_at "
                        "FROM scene s JOIN content_series cs ON cs.id = s.series_id "
                        "LEFT JOIN content_template ct ON ct.id=s.template_id "
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
        # 功能:按公开标识读取内容版本和编辑状态。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:内容版本对象及完整快照;未找到对应记录时为 None。
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
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:内容版本对象及完整快照。
        return await ProductionStore(
            self._session_factory, require_review=self._require_review
        ).save_revision(revision, expected_version)

    async def get_discovery_config(self) -> DiscoveryConfig:
        # 功能:读取开放场景、系列预览和学习模块配置。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:发现页配置及配置版本。
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
        # 功能:按预期配置版本保存开放场景、系列预览和学习模块开关。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景、系列预览和学习模块的发现页配置。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:发现页配置及配置版本。
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
        # 功能:读取内容版本的管理端预览快照。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:管理端预览对象和内容快照;未找到对应记录时为 None。
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
        # 功能:汇总指定内容版本的发布检查项。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:词典引用一致性检查项的列表。
        return await ProductionStore(
            self._session_factory,
            require_review=self._require_review,
            prepare_asset=self._prepare_asset,
        ).checks(revision_id)

    async def publish(
        self, revision: SceneRevision, actor_id: str, idempotency_key: str, published_at: datetime
    ) -> PublishedScene:
        # 功能:校验版本和内容后发布场景快照并保存幂等回执。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     revision: 场景内容版本对象,含快照、编辑版本和状态。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        #     published_at: 内容发布生效时间,同时写入发布记录和幂等响应。
        # 返回:已发布场景、版本标识和发布时间。
        return await ProductionStore(
            self._session_factory,
            require_review=self._require_review,
            prepare_asset=self._prepare_asset,
        ).publish(revision, actor_id, idempotency_key, published_at)

    async def current_open_config(self) -> OpenSceneConfig | None:
        # 功能:读取当前生效的开放场景配置。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:开放场景配置及生效时间;未找到对应记录时为 None。
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
        # 功能:保存当前开放场景和配置生效信息。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 开放场景标识及生效时间的配置对象。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:保存指定系列的预览场景配置。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     config: 指定系列的预览场景及生效时间配置。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:保存场景状态和当前内容版本引用。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene: 场景领域对象,含系列和当前草稿、发布版本引用。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE scene SET status = :status WHERE public_id = :scene_id"),
                {"status": scene.status, "scene_id": scene.id},
            )

    async def is_open(self, scene_id: str, now: datetime) -> bool:
        # 功能:判断场景是否属于当前启用的开放场景。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     now: 访问校验时的当前时间,当前查询仅核对启用配置,不按该时间过滤。
        # 返回:该场景是否位于已启用的开放场景配置内。
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
        # 功能:判断场景是否属于当前启用的系列预览。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     now: 访问校验时的当前时间,当前查询仅核对启用配置,不按该时间过滤。
        # 返回:该场景是否位于已启用的系列预览配置内。
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
        # 功能:读取发现页学习模块及启用状态。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:学习模块类型及启用状态的列表。
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT module_type, display_name, sort_order, entry_path "
                        "FROM learning_module_config WHERE enabled = 1 ORDER BY sort_order"
                    )
                )
            ).all()
        return [
            {
                **dict(row._mapping),
                "key": row.module_type,
                "title": row.display_name,
                "public_id": f"module-{row.module_type}",
                "enabled": True,
            }
            for row in rows
        ]

    async def learning_catalog(self, user_id: str) -> list[dict[str, object]]:
        # 功能:读取已发布场景的学习目录和试读句子。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     user_id: 学习目录请求的用户公开标识;当前目录查询面向所有已发布场景,不按用户筛选。
        # 返回:已发布场景的目录字段及首句试读内容列表。
        del user_id
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT s.public_id, s.title, s.summary, cs.title AS series, "
                        "s.cover_object_key, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_en')) AS title_en, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_zh')) AS title_zh, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.dialogue[0].english')) "
                        "AS trial_sentence "
                        "FROM scene s JOIN content_series cs ON cs.id = s.series_id "
                        "JOIN scene_revision r ON r.id=s.published_revision_id "
                        "WHERE s.status = 'PUBLISHED' "
                        "ORDER BY cs.sort_order, s.id"
                    )
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    async def entitlements(self, user_id: str, now: datetime) -> dict[str, object]:
        # 功能:投影用户正式及限时权益、固定场景成员和当前有效状态。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     user_id: 访问学习内容的用户公开标识,供权益查询和访问授权。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:正式及限时权益列表、权益版本摘要和服务端当前时间。
        """Project the user's entitlements and fixed scene membership at the given time."""
        async with self._session_factory() as session:
            formal_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT e.public_id AS id,p.public_id AS content_pack_id,"
                            "p.name AS title,"
                            "e.status,e.granted_at AS effective_at,e.expires_at,e.version "
                            "FROM formal_entitlement e JOIN content_package p ON p.id=e.package_id "
                            "JOIN user_account u ON u.id=e.user_id WHERE u.public_id=:user "
                            "ORDER BY e.id DESC"
                        ),
                        {"user": user_id},
                    )
                )
                .mappings()
                .all()
            )
            limited_rows = (
                (
                    await session.execute(
                        text(
                            "SELECT e.public_id AS id,c.public_id AS activity_id,"
                            "c.name AS title,e.status,e.start_deadline AS starts_before,"
                            "e.activated_at,e.expires_at,e.version,"
                            "v.duration_days,v.id AS version_internal_id "
                            ",u.id AS user_internal_id "
                            "FROM limited_entitlement e JOIN limited_campaign_version v "
                            "ON v.id=e.campaign_version_id "
                            "JOIN limited_campaign c ON c.id=v.campaign_id "
                            "JOIN user_account u ON u.id=e.user_id "
                            "WHERE u.public_id=:user ORDER BY e.id DESC"
                        ),
                        {"user": user_id},
                    )
                )
                .mappings()
                .all()
            )
            formal = [dict(row) for row in formal_rows]
            limited = [dict(row) for row in limited_rows]
            for item in limited:
                version_id = item.pop("version_internal_id")
                internal_user = item.pop("user_internal_id")
                item["achievements"] = await limited_achievements(
                    session,
                    internal_user,
                    version_id,
                    item["activated_at"],
                    item["expires_at"],
                    now,
                )
                scene_ids: Sequence[str] = (
                    (
                        await session.execute(
                            text(
                                "SELECT s.public_id FROM limited_campaign_scene cs "
                                "JOIN scene s ON s.id=cs.scene_id "
                                "WHERE cs.campaign_version_id=:version ORDER BY cs.position"
                            ),
                            {"version": version_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                item["scene_ids"] = list(scene_ids)
                item["scene_count"] = len(scene_ids)
        for item in formal + limited:
            expiry = _utc(item["expires_at"]) if item["expires_at"] else None
            start = _utc(item["starts_before"]) if item.get("starts_before") else None
            if item["status"] == "PENDING" and start and now >= start:
                item["status"] = "START_EXPIRED"
            elif item["status"] in {"ACTIVE", "PAUSED"} and expiry and now >= expiry:
                item["status"] = "EXPIRED" if item in formal else "ENDED"
        version = hashlib.sha256(
            json.dumps([formal, limited], default=str, sort_keys=True).encode()
        ).hexdigest()[:24]
        return {
            "formal": formal,
            "limited": limited,
            "version": version,
            "authorization_pending": False,
            "server_now": now,
        }

    async def get_full_scene(self, scene_id: str) -> dict[str, object] | None:
        # 功能:读取已发布场景的完整学习内容。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:已发布场景完整内容;不存在时为 None。
        return await ProductionStore(
            self._session_factory, require_review=self._require_review
        ).full_scene(scene_id)

    async def get_preview_scene(self, scene_id: str) -> dict[str, object] | None:
        # 功能:读取启用的场景预览并校验封面可用状态。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:启用的场景预览字段;未匹配到预览时为 None。
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT s.public_id, s.title, cs.title AS series, s.cover_object_key, "
                        "m.status AS cover_status, m.security_status AS cover_security_status, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_en')) AS title_en, "
                        "JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_zh')) AS title_zh, "
                        "p.introduction, 'PREVIEW' AS preview_status FROM preview_config p "
                        "JOIN scene s ON s.id = p.scene_id JOIN content_series cs "
                        "ON cs.id = s.series_id JOIN scene_revision r "
                        "ON r.id=s.published_revision_id LEFT JOIN media_asset m "
                        "ON m.object_key=s.cover_object_key WHERE s.public_id = :scene_id "
                        "AND p.enabled = 1 AND s.status='PUBLISHED'"
                    ),
                    {"scene_id": scene_id},
                )
            ).first()
        if row is None:
            return None
        preview = dict(row._mapping)
        cover_status = preview.pop("cover_status")
        cover_security_status = preview.pop("cover_security_status")
        if preview["cover_object_key"] and (
            cover_status != "CONFIRMED"
            or not security_status_usable(
                cover_security_status, require_review=self._require_review
            )
        ):
            raise AppError("RESOURCE_NOT_READY", "封面资源尚未通过检查", 403)
        return preview

    async def get_entry(self, scene_id: str, entry_id: str) -> dict[str, object] | None:
        # 功能:读取已发布场景内指定稳定标识的词条。
        # 参数:
        #     self: 当前 SQLAlchemyContentRepository 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     entry_id: 词汇或语块的稳定词条标识,关联词典或场景词条。
        # 返回:已发布场景词条的英文、音标、中文和解释;不存在时为 None。
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
    # 功能:解析 JSON 内容,仅接受字典结构,其余返回空字典。
    # 参数:
    #     value: 数据库 JSON 字段的原始字符串或已解码结构,转换为空值安全的字典。
    # 返回:解析出的内容字典;内容不是字典时为空字典。
    decoded = json.loads(value) if isinstance(value, str) else value
    return dict(decoded) if isinstance(decoded, dict) else {}


def _utc(value: datetime) -> datetime:
    # 功能:保留已有时区,为无时区数据库时间补充 UTC 标记。
    # 参数:
    #     value: 数据库返回的时间值,已有时区保留,无时区时按 UTC 标记。
    # 返回:保留原有时区或补充 UTC 时区后的时间。
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _scene_from_row(row: object) -> Scene:
    # 功能:将关联查询行转换为场景领域对象。
    # 参数:
    #     row: 数据库查询行,包含构造场景对象及当前版本引用所需的字段。
    # 返回:场景对象及当前版本引用。
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
        template_type=getattr(row, "template_type", "dialogue") or "dialogue",
        updated_at=_utc(row.updated_at),  # type: ignore[attr-defined]
    )


def _revision_conflict(revision: SceneRevision) -> AppError:
    # 功能:构造包含当前草稿标识和编辑版本的并发冲突错误。
    # 参数:
    #     revision: 场景内容版本对象,含快照、编辑版本和状态。
    # 返回:包含当前草稿版本的并发冲突业务异常对象。
    return AppError(
        "REVISION_VERSION_CONFLICT",
        "内容草稿已被其他管理员更新",
        409,
        {"current_revision_id": revision.id, "current_version": revision.version},
    )
