from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Scene:
    id: str
    series_id: str
    title: str = ""
    series_title: str = ""
    summary: str | None = None
    cover_object_key: str | None = None
    status: str = "DRAFT"
    draft_revision_id: str | None = None
    published_revision_id: str | None = None
    updated_at: datetime | None = None
    template_type: str = "dialogue"


@dataclass(slots=True)
class SceneRevision:
    id: str
    scene_id: str
    source_revision_id: str | None
    version: int = 1
    status: str = "DRAFT"
    stable_sentence_ids: tuple[str, ...] = ()
    stable_entry_ids: tuple[str, ...] = ()
    content: dict[str, object] = field(default_factory=dict)
    created_by: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublishCheck:
    code: str
    severity: str
    passed: bool


@dataclass(frozen=True, slots=True)
class PublishCheckSummary:
    revision_id: str
    ready: bool
    error_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublishedScene:
    scene_id: str
    revision_id: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class OpenSceneConfig:
    version: int
    scene_ids: tuple[str, str, str]
    activated_at: datetime
    actor_id: str


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    series_id: str
    scene_ids: tuple[str, ...]
    updated_at: datetime
    actor_id: str


@dataclass(frozen=True, slots=True)
class ScenePage:
    items: tuple[Scene, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    version: int
    open_scene_ids: tuple[str, ...]
    preview_by_series: dict[str, tuple[str, ...]]
    learning_modules: dict[str, bool]
    updated_at: datetime | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdminPreview:
    scene_id: str
    revision_id: str
    revision_status: str
    scene_title: str
    series_title: str
    content: dict[str, object]
