from datetime import datetime
from typing import Protocol

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
