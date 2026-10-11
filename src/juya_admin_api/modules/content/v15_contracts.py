"""Typed internal V1.5 contract. Public BFF DTOs add the authenticated user's state."""

from typing import Literal

from pydantic import Field, model_validator

from juya_admin_api.modules.content.schemas import ContentModel

ContentType = Literal["dialogue", "word_card", "short_reading"]
EntryType = Literal["word", "chunk"]
IMPORT_NAMESPACE = "juya-content-library"


def entry_type(value: str) -> EntryType:
    if value in {"VOCABULARY", "word"}:
        return "word"
    if value in {"PHRASE", "chunk"}:
        return "chunk"
    raise ValueError("Unknown entry type")


def legacy_entry_type(value: EntryType) -> Literal["VOCABULARY", "PHRASE"]:
    return "VOCABULARY" if value == "word" else "PHRASE"


def content_type(value: str) -> ContentType:
    if value == "vocabulary":
        return "word_card"
    if value in {"dialogue", "word_card", "short_reading"}:
        return value  # type: ignore[return-value]
    raise ValueError("Unknown content type")


class ResourceReference(ContentModel):
    target_id: str
    resource_version: str
    duration_ms: int = Field(gt=0)


class PronunciationReference(ContentModel):
    target_id: str
    can_prepare: bool = False
    resource_version: str | None = None


class SourceLocator(ContentModel):
    sentence_id: str | None = None
    paragraph_id: str | None = None


class ConfirmedEntry(ContentModel):
    entry_id: str = Field(min_length=1, max_length=64)
    entry_type: EntryType
    sense_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    entry_version: int = Field(default=1, ge=1)
    english: str = Field(min_length=1, max_length=10000)
    phonetic: str = Field(default="", max_length=200)
    meaning_zh: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(default="", max_length=10000)
    source_sentence: str = Field(max_length=10000)
    source_locator: SourceLocator | None = None
    image_asset_id: str | None = None
    pronunciation: PronunciationReference | None = None
    legacy_incomplete: bool = False

    @model_validator(mode="after")
    def chunk_has_no_image(self) -> "ConfirmedEntry":
        if self.entry_type == "chunk" and self.image_asset_id is not None:
            raise ValueError("Chunks do not accept display images")
        return self


class PublishedSentence(ContentModel):
    sentence_id: str
    speaker: str
    english: str = Field(max_length=10000)
    chinese: str = Field(max_length=10000)
    position: int = Field(ge=1)
    start_ms: int | None = None
    end_ms: int | None = None
    slice_target_id: str | None = None
    slice_version: str | None = None


class SceneDetail(ContentModel):
    recording_enabled: bool = False
    content_id: str
    content_type: Literal["dialogue", "word_card"]
    content_version: int = Field(ge=1)
    revision_id: str
    schema_version: Literal[2] = 2
    title_en: str
    title_zh: str
    dialogue: list[PublishedSentence] = Field(default_factory=list)
    words: list[ConfirmedEntry] = Field(default_factory=list)
    chunks: list[ConfirmedEntry] = Field(default_factory=list)
    whole_audio: ResourceReference | None = None
    cover_asset_id: str | None = None
    learning_image_asset_id: str | None = None


class ShortReadingDetail(ContentModel):
    content_id: str
    content_type: Literal["short_reading"] = "short_reading"
    content_version: int = Field(ge=1)
    revision_id: str
    schema_version: Literal[2] = 2
    title_en: str
    title_zh: str
    category: str
    topic: str
    level: str
    safe_intro: str
    paragraphs_en: list[str]
    paragraph_ids: list[str] = Field(default_factory=list)
    translation_zh: str
    whole_audio: ResourceReference | None = None
    cover_asset_id: str | None = None


class FavoriteReference(ContentModel):
    content_type: ContentType
    content_id: str
    content_version: int = Field(ge=1)
    entry_id: str
    sense_id: str
    source_id: str


class ConfirmationCredential(FavoriteReference):
    user_id: str
    revision_id: str
    details_hash: str
    module_version: int
    authorization_version: str
    expires_at: str
    token: str


class SourceResolution(ContentModel):
    saved_content_version: int | None
    target_content_version: int | None
    status: Literal["EXACT", "UPDATED_OR_UNRESOLVED", "NO_ACCESS"]
    locator: SourceLocator | None = None
    safe_summary: dict[str, str]


class FavoriteResourceEvent(ContentModel):
    event_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    favorite_id: str
    snapshot_revision_id: str
    active: bool
    content_type: ContentType
    content_id: str
    revision_id: str
    image_asset_id: str | None = None
    pronunciation_target_id: str | None = None
    resource_version: str | None = None
