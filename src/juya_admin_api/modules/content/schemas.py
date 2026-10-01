"""Shared V1.3 draft and published content contract. All draft fields may be incomplete."""

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioReference(ContentModel):
    target_id: str
    version_id: str
    asset_id: str
    duration_ms: int = Field(ge=0)


class ClickableSpan(ContentModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    entry_id: str
    entry_version: int = Field(ge=1)
    source_locator: str


class DialogueSentence(ContentModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    speaker: str = Field(default="", max_length=100)
    english: str = Field(default="", max_length=10000)
    chinese: str = Field(default="", max_length=10000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    audio_version_id: str | None = None
    timing_confirmed: bool = False
    clickable_spans: list[ClickableSpan] = Field(default_factory=list)


class SceneEntry(ContentModel):
    entry_id: str = ""
    entry_version: int = Field(default=1, ge=1)
    english: str = Field(default="", max_length=500)
    variants: list[str] = Field(default_factory=list, max_length=100)
    phonetic: str = Field(default="", max_length=200)
    chinese: str = Field(default="", max_length=10000)
    explanation: str = Field(default="", max_length=10000)
    source_sentence_ids: list[str] = Field(default_factory=list)
    icon_asset_id: str | None = None
    audio_target_id: str | None = None
    audio_version_id: str | None = None


class SceneContent(ContentModel):
    title_en: str = Field(default="", max_length=200)
    title_zh: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=100)
    original_image_asset_id: str | None = None
    cover_asset_id: str | None = None
    copyright: str = Field(default="", max_length=5000)
    source: str = Field(default="", max_length=5000)
    audio: AudioReference | None = None
    dialogue: list[DialogueSentence] = Field(default_factory=list, max_length=1000)
    vocabulary: list[SceneEntry] = Field(default_factory=list, max_length=1000)
    chunks: list[SceneEntry] = Field(default_factory=list, max_length=1000)


def normalize_audio_change(previous: SceneContent, proposed: SceneContent) -> SceneContent:
    """A replacement invalidates all old intervals, including maliciously retained confirmations."""
    result = proposed.model_copy(deep=True)
    before = previous.audio.version_id if previous.audio else None
    after = result.audio.version_id if result.audio else None
    if before is not None and before != after:
        for sentence in result.dialogue:
            sentence.start_ms = None
            sentence.end_ms = None
            sentence.audio_version_id = None
            sentence.timing_confirmed = False
    old_sentences = {sentence.id: sentence for sentence in previous.dialogue}
    for sentence in result.dialogue:
        old = old_sentences.get(sentence.id)
        if (
            old is not None
            and (old.english, old.start_ms, old.end_ms)
            != (sentence.english, sentence.start_ms, sentence.end_ms)
            and sentence.audio_version_id != after
        ):
            sentence.timing_confirmed = False
    return result
