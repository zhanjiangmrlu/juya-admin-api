import hashlib
from typing import ClassVar

import pytest

from juya_admin_api.modules.audio_pipeline.service import AudioPipelineService, SliceInput
from juya_admin_api.modules.audio_pipeline.slicing import Boundary, SliceResult


@pytest.mark.asyncio
async def test_late_output_cannot_replace_changed_boundary():
    original = SliceInput(
        "r1",
        "s1",
        "Hello",
        "key",
        "v1",
        hashlib.sha256(b"audio").hexdigest(),
        Boundary(0, 1000),
        "admin",
    )

    class Store:
        async def current(self, revision_id, sentence_id):
            return original

        async def find_ready(self, item):
            return None

        async def commit_if_current(self, item, result, object_key):
            return None

    class Oss:
        async def read_bytes(self, key, max_bytes):
            return b"audio"

        async def freeze_bytes(self, data, asset_type, content_type):
            return "sealed/media/immutable.wav"

    class Slicer:
        encoding_config: ClassVar[dict[str, object]] = {}

        def slice(self, data, digest, boundary):
            return SliceResult(b"slice", 1000, "digest")

    with pytest.raises(ValueError, match="dependencies changed"):
        await AudioPipelineService(Oss(), Slicer(), Store()).split("r1", "s1")


@pytest.mark.asyncio
async def test_unverified_boundaries_never_read_source():
    original = SliceInput("r1", "s1", "Hello", "key", "v1", "hash", Boundary(0, 1000), "")

    class Store:
        async def current(self, revision_id, sentence_id):
            return original

    with pytest.raises(ValueError, match="verified"):
        await AudioPipelineService(None, None, Store()).split("r1", "s1")
