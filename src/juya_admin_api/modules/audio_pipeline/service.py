import asyncio
from dataclasses import dataclass
from typing import Protocol

from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.modules.audio_pipeline.slicing import (
    Boundary,
    FFmpegSlicer,
    SliceResult,
    dependency_hash,
)


@dataclass(frozen=True)
class SliceInput:
    revision_id: str
    sentence_id: str
    english: str
    source_object_key: str
    source_audio_version: str
    source_sha256: str
    boundary: Boundary
    verified_by: str
    content_type: str = "scene"
    content_id: str = ""

    def dependency(self, encoding: dict[str, object]) -> str:
        return dependency_hash(
            sentence_id=self.sentence_id,
            english=self.english,
            source_sha256=self.source_sha256,
            source_audio_version=self.source_audio_version,
            boundary=self.boundary,
            encoding_config=encoding,
        )


@dataclass(frozen=True)
class SavedSlice:
    id: str
    asset_id: str
    target_id: str
    resource_version: str
    dependency_hash: str
    status: str
    actual_duration_ms: int


class SliceRepository(Protocol):
    async def current(self, revision_id: str, sentence_id: str) -> SliceInput: ...
    async def find_ready(self, item: SliceInput) -> SavedSlice | None: ...
    async def commit_if_current(
        self, item: SliceInput, result: SliceResult, object_key: str
    ) -> SavedSlice | None: ...


class AudioPipelineService:
    def __init__(self, oss: OssProvider, slicer: FFmpegSlicer, repository: SliceRepository) -> None:
        self.oss = oss
        self.slicer = slicer
        self.repository = repository

    async def split(self, revision_id: str, sentence_id: str) -> SavedSlice:
        item = await self.repository.current(revision_id, sentence_id)
        if not item.verified_by:
            raise ValueError("sentence boundaries must be verified")
        existing = await self.repository.find_ready(item)
        if existing is not None:
            if await self.repository.current(revision_id, sentence_id) != item:
                raise ValueError("audio dependencies changed during cache lookup")
            return existing
        source = await self.oss.read_bytes(item.source_object_key, 50 * 1024 * 1024)
        result = await asyncio.to_thread(
            self.slicer.slice, source, item.source_sha256, item.boundary
        )
        # A stale job may leave an orphan immutable object; never delete an old publication's file.
        object_key = await self.oss.freeze_bytes(result.data, "audio", result.content_type)
        saved = await self.repository.commit_if_current(item, result, object_key)
        if saved is None:
            raise ValueError("audio dependencies changed during processing")
        return saved
