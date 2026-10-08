from copy import deepcopy
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
from juya_admin_api.modules.content.schemas import SceneContent, normalize_audio_change
from juya_admin_api.modules.content.text_spans import build_clickable_spans
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class ContentService:
    def __init__(self, repository: ContentRepository) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     repository: 内容仓储,读写场景、版本、发现页配置和发布快照。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self._repository = repository

    async def list_revision_history(
        self, scene_id: str, *, page: int = 1, page_size: int = 20
    ) -> dict[str, object]:
        # 功能:分页读取场景的内容版本历史。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        # 返回:内容版本列表及分页元信息。
        await self.get_scene(scene_id)
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("PAGINATION_INVALID", "分页参数无效", 422)
        return await self._repository.list_revision_history(scene_id, page, page_size)

    async def create_revision(
        self,
        scene_id: str,
        source_revision_id: str | None,
        actor_id: str,
        now: datetime,
    ) -> SceneRevision:
        # 功能:从指定来源版本复制内容,创建新的场景草稿。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     source_revision_id: 复制草稿的来源版本公开标识;为空时创建空内容草稿。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:内容版本对象及完整快照。
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
            content={} if source is None else deepcopy(source.content),
            created_by=actor_id,
            created_at=now,
        )
        await self._repository.save_revision(revision)
        return revision

    async def update_revision_content(
        self, revision_id: str, content: dict[str, object]
    ) -> SceneRevision:
        # 功能:确认版本可编辑并按当前编辑版本保存新内容。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     content: 待保存的场景内容快照字典,按结构化内容模型校验。
        # 返回:内容版本对象及完整快照。
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

    async def inspect_publish(
        self,
        revision_id: str,
        acknowledged_warning_codes: frozenset[str],
    ) -> PublishCheckSummary:
        # 功能:汇总发布错误和提醒,计算当前版本是否已具备发布条件。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     acknowledged_warning_codes: 管理员已确认的发布提醒代码集合。
        # 返回:发布就绪状态、错误代码及提醒代码摘要。
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
        ready = not errors and all(code in acknowledged_warning_codes for code in warnings)
        return PublishCheckSummary(revision_id, ready, errors, warnings)

    async def validate_publish(
        self,
        revision_id: str,
        acknowledged_warning_codes: frozenset[str],
    ) -> PublishCheckSummary:
        # 功能:校验发布错误和提醒确认情况,未满足条件时阻止发布。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     acknowledged_warning_codes: 管理员已确认的发布提醒代码集合。
        # 返回:发布就绪状态、错误代码及提醒代码摘要。
        summary = await self.inspect_publish(revision_id, acknowledged_warning_codes)
        if summary.error_codes:
            raise AppError(
                "PUBLISH_CHECK_FAILED",
                "内容未通过发布检查",
                409,
                {"error_codes": list(summary.error_codes)},
            )
        unacknowledged = tuple(
            code for code in summary.warning_codes if code not in acknowledged_warning_codes
        )
        if unacknowledged:
            raise AppError(
                "PUBLISH_WARNING_NOT_ACKNOWLEDGED",
                "发布提醒尚未确认",
                409,
                {"warning_codes": list(unacknowledged)},
            )
        return summary

    async def publish_revision(
        self,
        revision_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        *,
        acknowledged_warning_codes: frozenset[str] = frozenset(),
        expected_version: int | None = None,
    ) -> PublishedScene:
        # 功能:核对草稿编辑版本和发布条件后发布内容。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     idempotency_key: 请求幂等键,重复业务请求据此复用执行结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        #     acknowledged_warning_codes: 管理员已确认的发布提醒代码集合。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        # 返回:已发布场景、版本标识和发布时间。
        revision = await self._require_revision(revision_id)
        if expected_version is not None and revision.version != expected_version:
            raise AppError("REVISION_VERSION_CONFLICT", "内容草稿已被其他管理员更新", 409)
        await self.validate_publish(revision_id, acknowledged_warning_codes)
        return await self._repository.publish(revision, actor_id, idempotency_key, now)

    async def replace_open_scenes(
        self,
        scene_ids: tuple[str, str, str],
        actor_id: str,
        now: datetime,
    ) -> OpenSceneConfig:
        # 功能:校验三个不同的已发布场景并替换开放配置。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     scene_ids: 三个互不重复的已发布场景标识,作为开放场景配置。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:开放场景配置及生效时间。
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
        # 功能:校验本系列三至六个已发布场景并替换预览配置。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     scene_ids: 本系列三至六个互不重复的已发布场景标识,不得包含开放场景。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:系列预览配置及生效时间。
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
        # 功能:校验场景未被开放或预览配置引用后将其下线。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        #     actor_id: 下线操作的管理员标识,当前服务保留该参数但不写入记录。
        #     now: 下线操作时间,当前服务保留该参数但不写入记录。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:读取必须存在的内容版本,不存在时抛出业务错误。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:内容版本对象及完整快照。
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
        # 功能:按系列、状态和检索条件分页读取场景。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     page: 从 1 开始的请求页码。
        #     page_size: 每页记录数,管理列表约束为 1 至 100。
        #     query: 检索关键词;为空或空字符串时不按关键词过滤。
        #     series_id: 内容系列公开标识,限定场景归属或筛选范围。
        #     status: 待写入或筛选的业务状态代码。
        # 返回:场景分页记录和总数。
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
        # 功能:按公开标识读取场景及其当前版本引用。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     scene_id: 场景公开标识,定位场景及其内容版本。
        # 返回:场景对象及当前版本引用。
        scene = await self._repository.get_scene(scene_id)
        if scene is None:
            raise AppError("SCENE_NOT_FOUND", "场景不存在", 404)
        return scene

    async def get_revision(self, revision_id: str) -> SceneRevision:
        # 功能:按公开标识读取内容版本和编辑状态。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:内容版本对象及完整快照。
        return await self._require_revision(revision_id)

    async def save_revision(
        self,
        revision_id: str,
        content: dict[str, object],
        *,
        expected_version: int,
        actor_id: str,
    ) -> SceneRevision:
        # 功能:按预期编辑版本保存草稿和结构化内容引用。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        #     content: 待保存的场景内容快照字典,按结构化内容模型校验。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        # 返回:内容版本对象及完整快照。
        revision = await self._require_revision(revision_id)
        if revision.status not in {"DRAFT", "REVIEWED", "PUBLISH_READY"}:
            raise AppError("REVISION_NOT_EDITABLE", "当前内容版本不可编辑", 409)
        if revision.version != expected_version:
            raise AppError(
                "REVISION_VERSION_CONFLICT",
                "内容草稿已被其他管理员更新",
                409,
                {"current_revision_id": revision.id, "current_version": revision.version},
            )
        normalized = normalize_audio_change(
            SceneContent.model_validate(revision.content), SceneContent.model_validate(content)
        )
        normalized = build_clickable_spans(normalized)
        updated = replace(revision, content=normalized.model_dump(mode="json"), created_by=actor_id)
        return await self._repository.save_revision(updated, expected_version)

    async def get_discovery_config(self) -> DiscoveryConfig:
        # 功能:读取开放场景、系列预览和学习模块配置。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        # 返回:发现页配置及配置版本。
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
        # 功能:按预期配置版本保存开放场景、系列预览和学习模块开关。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     open_scene_ids: 恰好三个互不重复的已发布开放场景标识。
        #     preview_by_series: 按系列分组的预览场景标识,每系列三至六个且不与开放场景重复。
        #     learning_modules: 学习模块类型到启用状态的映射,目前仅允许启用场景学习。
        #     expected_version: 客户端读取时的编辑或配置版本号,保存时核对以避免并发覆盖。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:发现页配置及配置版本。
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
        # 功能:读取内容版本的管理端预览快照。
        # 参数:
        #     self: 当前 ContentService 实例,持有本方法访问的依赖和业务状态。
        #     revision_id: 场景内容版本公开标识,定位待编辑、检查或访问的快照。
        # 返回:管理端预览对象和内容快照。
        await self._require_revision(revision_id)
        preview = await self._repository.admin_preview(revision_id)
        if preview is None:
            raise AppError("REVISION_NOT_FOUND", "内容版本不存在", 404)
        return preview
