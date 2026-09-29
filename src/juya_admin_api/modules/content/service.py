from dataclasses import replace
from datetime import datetime

from juya_admin_api.modules.content.domain import (
    AdminPreview,
    DiscoveryConfig,
    OpenSceneConfig,
    PreviewConfig,
    PublishCheckSummary,
    PublishedScene,
    Scene,
    ScenePage,
    SceneRevision,
)
from juya_admin_api.modules.content.repository import ContentRepository
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class ContentService:
    def __init__(self, repository: ContentRepository) -> None:
        self._repository = repository

    async def create_revision(
        self,
        scene_id: str,
        source_revision_id: str | None,
        actor_id: str,
        now: datetime,
    ) -> SceneRevision:
        scene = await self._repository.get_scene(scene_id)
        if scene is None:
            raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
        source = None
        if source_revision_id is not None:
            source = await self._repository.get_revision(source_revision_id)
            if source is None or source.scene_id != scene_id:
                raise AppError("SOURCE_REVISION_INVALID", "来源版本无效", 422)
        revision = SceneRevision(
            id=new_ulid(now),
            scene_id=scene_id,
            source_revision_id=source_revision_id,
            stable_sentence_ids=() if source is None else source.stable_sentence_ids,
            stable_entry_ids=() if source is None else source.stable_entry_ids,
            content={} if source is None else dict(source.content),
            created_by=actor_id,
            created_at=now,
        )
        await self._repository.save_revision(revision)
        return revision

    async def update_revision_content(
        self, revision_id: str, content: dict[str, object]
    ) -> SceneRevision:
        revision = await self._require_revision(revision_id)
        if revision.status in {"PUBLISHED", "SUPERSEDED"}:
            raise AppError(
                "PUBLISHED_REVISION_IMMUTABLE",
                "已发布版本不可原地修改",
                409,
            )
        return await self.save_revision(
            revision_id,
            content,
            expected_version=revision.version,
            actor_id=revision.created_by,
        )

    async def validate_publish(
        self,
        revision_id: str,
        acknowledged_warning_codes: frozenset[str],
    ) -> PublishCheckSummary:
        await self._require_revision(revision_id)
        checks = await self._repository.list_publish_checks(revision_id)
        errors = tuple(
            sorted(check.code for check in checks if check.severity == "ERROR" and not check.passed)
        )
        warnings = tuple(
            sorted(
                check.code for check in checks if check.severity == "WARNING" and not check.passed
            )
        )
        if errors:
            raise AppError(
                "PUBLISH_CHECK_FAILED",
                "内容未通过发布检查",
                409,
                {"error_codes": list(errors)},
            )
        unacknowledged = tuple(code for code in warnings if code not in acknowledged_warning_codes)
        if unacknowledged:
            raise AppError(
                "PUBLISH_WARNING_NOT_ACKNOWLEDGED",
                "发布提醒尚未确认",
                409,
                {"warning_codes": list(unacknowledged)},
            )
        return PublishCheckSummary(revision_id, True, errors, warnings)

    async def publish_revision(
        self,
        revision_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        *,
        acknowledged_warning_codes: frozenset[str] = frozenset(),
    ) -> PublishedScene:
        revision = await self._require_revision(revision_id)
        await self.validate_publish(revision_id, acknowledged_warning_codes)
        return await self._repository.publish(revision, actor_id, idempotency_key, now)

    async def replace_open_scenes(
        self,
        scene_ids: tuple[str, str, str],
        actor_id: str,
        now: datetime,
    ) -> OpenSceneConfig:
        if len(scene_ids) != 3 or len(set(scene_ids)) != 3:
            raise AppError(
                "OPEN_SCENES_INVALID",
                "开放场景必须恰好包含 3 个不同场景",
                422,
            )
        for scene_id in scene_ids:
            scene = await self._repository.get_scene(scene_id)
            if scene is None or scene.status != "PUBLISHED" or scene.published_revision_id is None:
                raise AppError(
                    "OPEN_SCENE_NOT_PUBLISHED",
                    "开放配置只能引用已发布场景",
                    409,
                )
        discovery = await self._repository.get_discovery_config()
        saved = await self.save_discovery_config(
            open_scene_ids=scene_ids,
            preview_by_series=discovery.preview_by_series,
            learning_modules=discovery.learning_modules,
            expected_version=discovery.version,
            actor_id=actor_id,
            now=now,
        )
        return OpenSceneConfig(
            version=saved.version,
            scene_ids=scene_ids,
            activated_at=now,
            actor_id=actor_id,
        )

    async def replace_preview_scenes(
        self,
        series_id: str,
        scene_ids: tuple[str, ...],
        actor_id: str,
        now: datetime,
    ) -> PreviewConfig:
        if not 3 <= len(scene_ids) <= 6 or len(scene_ids) != len(set(scene_ids)):
            raise AppError(
                "PREVIEW_SCENES_INVALID",
                "预览配置必须包含 3 至 6 个不同场景",
                422,
            )
        open_config = await self._repository.current_open_config()
        open_ids = frozenset(() if open_config is None else open_config.scene_ids)
        for scene_id in scene_ids:
            scene = await self._repository.get_scene(scene_id)
            if scene_id in open_ids:
                raise AppError("PREVIEW_SCENE_IS_OPEN", "开放场景不能重复配置为预览", 409)
            if (
                scene is None
                or scene.series_id != series_id
                or scene.status != "PUBLISHED"
                or scene.published_revision_id is None
            ):
                raise AppError(
                    "PREVIEW_SCENE_NOT_PUBLISHED",
                    "预览配置只能引用本系列已发布场景",
                    409,
                )
        discovery = await self._repository.get_discovery_config()
        preview_by_series = dict(discovery.preview_by_series)
        preview_by_series[series_id] = scene_ids
        if open_config is not None:
            open_scene_ids = open_config.scene_ids
        elif len(discovery.open_scene_ids) == 3:
            open_scene_ids = (
                discovery.open_scene_ids[0],
                discovery.open_scene_ids[1],
                discovery.open_scene_ids[2],
            )
        else:
            raise AppError(
                "OPEN_SCENES_REQUIRED",
                "请先配置三个开放场景",
                409,
            )
        await self.save_discovery_config(
            open_scene_ids=open_scene_ids,
            preview_by_series=preview_by_series,
            learning_modules=discovery.learning_modules,
            expected_version=discovery.version,
            actor_id=actor_id,
            now=now,
        )
        return PreviewConfig(series_id, scene_ids, now, actor_id)

    async def offline_scene(self, scene_id: str, actor_id: str, now: datetime) -> None:
        del actor_id, now
        scene = await self._repository.get_scene(scene_id)
        if scene is None:
            raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
        open_config = await self._repository.current_open_config()
        if open_config is not None and scene_id in open_config.scene_ids:
            raise AppError(
                "OPEN_SCENE_CANNOT_OFFLINE",
                "当前开放场景不可下线, 请先替换开放配置",
                409,
            )
        discovery = await self._repository.get_discovery_config()
        if any(scene_id in scene_ids for scene_ids in discovery.preview_by_series.values()):
            raise AppError(
                "PREVIEW_SCENE_CANNOT_OFFLINE",
                "当前系列预览场景不可下线, 请先替换预览配置",
                409,
            )
        scene.status = "OFFLINE"
        await self._repository.save_scene(scene)

    async def _require_revision(self, revision_id: str) -> SceneRevision:
        revision = await self._repository.get_revision(revision_id)
        if revision is None:
            raise AppError("REVISION_NOT_FOUND", "内容版本不存在", 404)
        return revision

    async def list_scenes(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str | None = None,
        series_id: str | None = None,
        status: str | None = None,
    ) -> ScenePage:
        if page < 1 or page_size < 1 or page_size > 100:
            raise AppError("PAGINATION_INVALID", "分页参数不正确", 422)
        if status is not None and status not in {"DRAFT", "PUBLISHED", "OFFLINE"}:
            raise AppError("SCENE_STATUS_INVALID", "场景状态不正确", 422)
        return await self._repository.list_scenes(
            page=page,
            page_size=page_size,
            query=query,
            series_id=series_id,
            status=status,
        )

    async def get_scene(self, scene_id: str) -> Scene:
        scene = await self._repository.get_scene(scene_id)
        if scene is None:
            raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
        return scene

    async def get_revision(self, revision_id: str) -> SceneRevision:
        return await self._require_revision(revision_id)

    async def save_revision(
        self,
        revision_id: str,
        content: dict[str, object],
        *,
        expected_version: int,
        actor_id: str,
    ) -> SceneRevision:
        revision = await self._require_revision(revision_id)
        if revision.status not in {"DRAFT", "REVIEWED", "PUBLISH_READY"}:
            raise AppError("REVISION_NOT_EDITABLE", "当前内容版本不可编辑", 409)
        updated = replace(revision, content=dict(content), created_by=actor_id)
        return await self._repository.save_revision(updated, expected_version)

    async def get_discovery_config(self) -> DiscoveryConfig:
        return await self._repository.get_discovery_config()

    async def save_discovery_config(
        self,
        *,
        open_scene_ids: tuple[str, str, str],
        preview_by_series: dict[str, tuple[str, ...]],
        learning_modules: dict[str, bool],
        expected_version: int,
        actor_id: str,
        now: datetime,
    ) -> DiscoveryConfig:
        current = await self._repository.get_discovery_config()
        if current.version != expected_version:
            raise AppError(
                "DISCOVERY_CONFIG_VERSION_CONFLICT",
                "发现页配置已被其他管理员更新",
                409,
                {"current_version": current.version},
            )
        if len(open_scene_ids) != 3 or len(set(open_scene_ids)) != 3:
            raise AppError("OPEN_SCENES_INVALID", "开放场景必须恰好包含 3 个不同场景", 422)
        if any(
            enabled and module_type != "scene_learning"
            for module_type, enabled in learning_modules.items()
        ):
            raise AppError(
                "LEARNING_MODULE_NOT_ALLOWED",
                "V1.3 仅允许启用场景学习",
                422,
            )
        for scene_id in open_scene_ids:
            scene = await self._repository.get_scene(scene_id)
            if scene is None or scene.status != "PUBLISHED" or scene.published_revision_id is None:
                raise AppError("OPEN_SCENE_NOT_PUBLISHED", "开放配置只能引用已发布场景", 409)
        open_ids = frozenset(open_scene_ids)
        for series_id, scene_ids in preview_by_series.items():
            if not 3 <= len(scene_ids) <= 6 or len(scene_ids) != len(set(scene_ids)):
                raise AppError("PREVIEW_SCENES_INVALID", "预览配置必须包含 3 至 6 个不同场景", 422)
            for scene_id in scene_ids:
                if scene_id in open_ids:
                    raise AppError("PREVIEW_SCENE_IS_OPEN", "开放场景不能重复配置为预览", 409)
                scene = await self._repository.get_scene(scene_id)
                if (
                    scene is None
                    or scene.series_id != series_id
                    or scene.status != "PUBLISHED"
                    or scene.published_revision_id is None
                ):
                    raise AppError(
                        "PREVIEW_SCENE_NOT_PUBLISHED",
                        "预览配置只能引用本系列已发布场景",
                        409,
                    )
        config = DiscoveryConfig(
            expected_version + 1,
            tuple(open_scene_ids),
            {key: tuple(value) for key, value in preview_by_series.items()},
            dict(learning_modules),
            now,
            actor_id,
        )
        return await self._repository.save_discovery_config(config, expected_version)

    async def admin_preview(self, revision_id: str) -> AdminPreview:
        await self._require_revision(revision_id)
        preview = await self._repository.admin_preview(revision_id)
        if preview is None:
            raise AppError("REVISION_NOT_FOUND", "内容版本不存在", 404)
        return preview
