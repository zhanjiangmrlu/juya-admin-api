from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from juya_admin_api.modules.audio_pipeline.service import SliceInput
from juya_admin_api.modules.audio_pipeline.slicing import Boundary
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore, validate_boundary
from juya_admin_api.shared.errors import AppError


def test_boundary_requires_actual_source_duration_and_explicit_manual_check():
    assert validate_boundary(0, 100, 100, True).end_ms == 100
    for args in ((0, 101, 100, True), (0, 100, 100, False), (-1, 1, 100, True)):
        with pytest.raises((AppError, ValueError)):
            validate_boundary(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_status", "security_status", "require_review", "allowed"),
    [
        ("READY", "PASSED", True, True),
        ("READY", "SKIPPED", False, True),
        ("CONFIRMED", "SKIPPED", False, True),
        ("READY", "SKIPPED", True, False),
        ("READY", "BLOCKED", False, False),
        ("READY", "PENDING", False, False),
        ("FAILED", "PASSED", False, False),
    ],
)
async def test_decoded_import_source_obeys_review_policy(
    version_status, security_status, require_review, allowed
):
    row = {
        "version_id": "source-version",
        "target_type": "WHOLE",
        "asset_id": "source-asset",
        "status": version_status,
        "asset_status": "CONFIRMED",
        "security_status": security_status,
        "object_key": "sealed/media/audio/source.wav",
        "sha256": "a" * 64,
        "duration_ms": 17600,
    }
    result = SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    store = SceneAuthoringStore(None, None, None)
    store.require_review = require_review
    content = SimpleNamespace(
        audio=SimpleNamespace(
            version_id="source-version", target_id="whole-target", asset_id="source-asset"
        )
    )
    if allowed:
        assert await store.source(session, content) == row
    else:
        with pytest.raises(AppError) as error:
            await store.source(session, content)
        assert error.value.code == "SOURCE_AUDIO_NOT_READY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("security_status", "require_review", "expected"),
    [
        ("SKIPPED", False, "READY_TO_LISTEN"),
        ("SKIPPED", True, "REVIEW_PENDING"),
        ("PASSED", True, "READY_TO_LISTEN"),
        ("BLOCKED", False, "FAILED"),
    ],
)
async def test_slice_refresh_obeys_policy_without_claiming_human_listening(
    security_status, require_review, expected
):
    saved = {
        "asset_id": "slice-asset",
        "target_id": "slice-target",
        "resource_version": "slice-v1",
        "asset_status": "CONFIRMED",
        "security_status": security_status,
        "duration_ms": 500,
        "actual_duration_ms": 500,
        "sha256": "b" * 64,
        "slice_sha256": "b" * 64,
        "object_key": "sealed/media/audio/slice.wav",
        "audio_status": "READY",
        "slice_status": "READY",
    }
    final = {
        "status": "CONFIRMED",
        "security_status": security_status,
        "sha256": "b" * 64,
        "duration_ms": 500,
    }
    results = [None, saved, final]
    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(mappings=lambda row=row: SimpleNamespace(first=lambda: row))
            for row in results
        ]
    )
    sessions = MagicMock()
    sessions.return_value.__aenter__.return_value = session
    store = SceneAuthoringStore(None, None, None, require_review=require_review)
    store.sessions = sessions
    sentence = SimpleNamespace(id="sentence", slice_version=None, slice_dependency_hash=None)
    content = SimpleNamespace(dialogue=[sentence])
    store.locked_revision = AsyncMock(return_value=({"edit_version": 1}, content))
    store.source = AsyncMock(return_value={})
    current = SliceInput(
        "revision",
        "sentence",
        "Hello",
        "source.wav",
        "source-version",
        "a" * 64,
        Boundary(0, 500),
        "admin",
        content_id="scene",
    )
    store.make_input = lambda *args: current
    result = await store.slice_state("revision", "sentence", 1)
    assert result["status"] == expected
    assert result["listening_confirmed"] is False
    assert result["security_status"] == security_status
