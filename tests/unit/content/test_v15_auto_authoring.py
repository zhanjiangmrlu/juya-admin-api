from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.shared.errors import AppError


def scene():
    return SceneContent.model_validate(
        {
            "schema_version": 2,
            "template_type": "dialogue",
            "title_en": "Hello",
            "title_zh": "你好",
            "audio": {
                "target_id": "whole",
                "version_id": "source",
                "asset_id": "asset",
                "duration_ms": 3000,
            },
            "dialogue": [
                {"id": "a", "speaker": "A", "english": "Hello there.", "chinese": "你好"},
                {"id": "b", "speaker": "B", "english": "Good morning.", "chinese": "早上好"},
            ],
        }
    )


def authoring():
    store = SceneAuthoringStore(None, None, None)
    store.locked_revision = AsyncMock(
        return_value=({"public_id": "revision", "edit_version": 4, "content_id": "scene"}, scene())
    )
    store.source = AsyncMock(
        return_value={
            "version_id": "source",
            "duration_ms": 3000,
            "object_key": "sealed/media/audio/source.wav",
            "sha256": "a" * 64,
        }
    )
    store.persist = AsyncMock(return_value={"edit_version": 5})
    store.enqueue_slice_within = AsyncMock(return_value="stage")
    return store


@pytest.mark.asyncio
async def test_verified_alignment_queues_real_slices_without_human_confirmation():
    store = authoring()
    parts = [
        {"sentence_id": "a", "start_ms": 150, "end_ms": 950},
        {"sentence_id": "b", "start_ms": 1300, "end_ms": 2750},
    ]
    assert await store.apply_alignment_within(None, "revision", 4, parts, "admin") == 5
    content = store.persist.call_args.args[2]
    assert [(row.start_ms, row.end_ms) for row in content.dialogue] == [(150, 950), (1300, 2750)]
    assert all(
        row.boundary_verified_by == "system:asr-alignment" and row.slice_version is None
        for row in content.dialogue
    )
    assert store.enqueue_slice_within.await_count == 2
    assert store.persist.call_args.args[4] == "content.boundary.auto_align"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parts",
    [
        [{"sentence_id": "a", "start_ms": 150, "end_ms": 950}],
        [
            {"sentence_id": "a", "start_ms": 1300, "end_ms": 2750},
            {"sentence_id": "b", "start_ms": 150, "end_ms": 950},
        ],
        [
            {"sentence_id": "a", "start_ms": 150, "end_ms": 1400},
            {"sentence_id": "b", "start_ms": 1300, "end_ms": 2750},
        ],
        [
            {"sentence_id": "a", "start_ms": 150, "end_ms": 950},
            {"sentence_id": "b", "start_ms": 1300, "end_ms": 3100},
        ],
        [
            {"sentence_id": "b", "start_ms": 150, "end_ms": 950},
            {"sentence_id": "a", "start_ms": 1300, "end_ms": 2750},
        ],
    ],
)
async def test_unreliable_alignment_never_writes_or_queues(parts):
    store = authoring()
    with pytest.raises(AppError):
        await store.apply_alignment_within(None, "revision", 4, parts, "admin")
    store.persist.assert_not_awaited()
    store.enqueue_slice_within.assert_not_awaited()
