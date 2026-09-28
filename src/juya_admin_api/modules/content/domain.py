from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Scene:
    id: str
    series_id: str
    status: str = "DRAFT"
    draft_revision_id: str | None = None
    published_revision_id: str | None = None


@dataclass(slots=True)
class SceneRevision:
    id: str
    scene_id: str
    source_revision_id: str | None
    status: str = "DRAFT"
    stable_sentence_ids: tuple[str, ...] = ()
    stable_entry_ids: tuple[str, ...] = ()
    content: dict[str, object] = field(default_factory=dict)


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
