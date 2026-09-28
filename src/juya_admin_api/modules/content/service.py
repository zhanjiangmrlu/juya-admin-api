from datetime import datetime

from juya_admin_api.modules.content.domain import (
    OpenSceneConfig,
    PreviewConfig,
    PublishCheckSummary,
    PublishedScene,
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
        del actor_id
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
        revision.content = dict(content)
        await self._repository.save_revision(revision)
        return revision

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
        current = await self._repository.current_open_config()
        config = OpenSceneConfig(
            version=1 if current is None else current.version + 1,
            scene_ids=scene_ids,
            activated_at=now,
            actor_id=actor_id,
        )
        await self._repository.save_open_config(config)
        return config

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
        config = PreviewConfig(series_id, scene_ids, now, actor_id)
        await self._repository.save_preview_config(config)
        return config

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
        scene.status = "OFFLINE"
        await self._repository.save_scene(scene)

    async def _require_revision(self, revision_id: str) -> SceneRevision:
        revision = await self._repository.get_revision(revision_id)
        if revision is None:
            raise AppError("REVISION_NOT_FOUND", "内容版本不存在", 404)
        return revision
