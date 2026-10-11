"""Independent pure-reading draft and publication validation."""

from pydantic import BaseModel, ConfigDict, Field


class ReadingAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: str
    version_id: str
    target_id: str
    duration_ms: int = Field(gt=0)
    listening_confirmed: bool = False
    confirmed_asset_id: str | None = None
    confirmed_version_id: str | None = None


class ReadingContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_en: str = Field(default="", max_length=200)
    title_zh: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=100)
    topic: str = Field(default="", max_length=100)
    level: str = Field(default="", max_length=100)
    safe_intro: str = Field(default="", max_length=500)
    paragraphs_en: list[str] = Field(default_factory=list, max_length=1000)
    paragraph_ids: list[str] = Field(default_factory=list, max_length=1000)
    translation_zh: str = Field(default="", max_length=500000)
    copyright_status: str = ""
    copyright_source: str = Field(default="", max_length=10000)
    cover_asset_id: str | None = None
    audio: ReadingAudio | None = None


def publication_errors(content: ReadingContent) -> list[str]:
    errors = []
    for name in (
        "title_en",
        "title_zh",
        "category",
        "topic",
        "level",
        "safe_intro",
        "translation_zh",
    ):
        if not getattr(content, name).strip():
            errors.append(name.upper() + "_REQUIRED")
    if not content.paragraphs_en or any(not p.strip() for p in content.paragraphs_en):
        errors.append("PARAGRAPHS_REQUIRED")
    if content.copyright_status != "CONFIRMED" or not content.copyright_source.strip():
        errors.append("COPYRIGHT_REQUIRED")
    audio = content.audio
    if audio and (
        not audio.listening_confirmed
        or audio.confirmed_asset_id != audio.asset_id
        or audio.confirmed_version_id != audio.version_id
    ):
        errors.append("AUDIO_LISTENING_REQUIRED")
    return errors
