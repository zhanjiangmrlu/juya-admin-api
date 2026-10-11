import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from juya_admin_api.modules.content_import.parser import ImportLimits
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError


def service_fixture(count=1):
    items = [
        {
            "item_id": str(i),
            "import_content_id": str(i),
            "operation": "CREATE",
            "mapping": None,
            "issues": [],
            "defaults": [],
            "resources": {},
            "entries": [],
        }
        for i in range(count)
    ]
    batch = {
        "status": "PREVIEW_READY",
        "content_type": "dialogue",
        "created_by": "actor",
        "import_mode": "ZIP",
        "preview_version": 1,
        "manifest": {"preview": {"hash": "hash", "items": items}},
    }
    result = Mock()
    result.first.return_value = SimpleNamespace(status="PENDING", result=None)
    session = AsyncMock()
    session.execute.return_value = result
    session.begin = Mock(return_value=AsyncMock())
    service = ImportService.__new__(ImportService)
    service.sessions = Mock(return_value=AsyncMock())
    service.sessions.return_value.__aenter__.return_value = session
    service.batch = AsyncMock(return_value=batch)
    service.original = AsyncMock(return_value=b"package")
    service.production = AsyncMock()
    service.production.creation_replay.return_value = None
    service.current_mapping = AsyncMock(return_value=None)
    service.save_item_within = AsyncMock(
        return_value={"content_id": "content", "revision_id": "revision"}
    )
    service.limits = ImportLimits()
    service.bind_media = AsyncMock()
    return service, items


@pytest.mark.asyncio
async def test_thousand_confirmation_items_unpack_original_package_only_once():
    service, items = service_fixture(1000)
    with patch(
        "juya_admin_api.modules.content_import.service.read_container", return_value={}
    ) as unpack:
        result = await service.confirm(
            "batch", "hash", 1, [item["item_id"] for item in items], [], [], "actor", "key"
        )
    assert result["status"] == "CONFIRMED" and len(result["items"]) == 1000
    unpack.assert_called_once()
    service.original.assert_awaited_once()
    assert service.save_item_within.await_count == 1000


@pytest.mark.asyncio
async def test_old_worker_lease_loss_does_not_write_failed_item_or_receipt():
    service, _ = service_fixture()
    service.assert_claim_within = AsyncMock(
        side_effect=AppError("IMPORT_STAGE_LEASE_LOST", "lost", 409)
    )
    claim = Claim("stage", 1, "hash", "v15.1", "worker")
    with (
        patch("juya_admin_api.modules.content_import.service.read_container", return_value={}),
        pytest.raises(AppError) as error,
    ):
        await service.confirm("batch", "hash", 1, ["0"], [], [], "actor", "key", claim)
    assert error.value.code == "IMPORT_STAGE_LEASE_LOST"
    service.save_item_within.assert_not_awaited()
    service.production.remember_creation.assert_not_awaited()
    # No success or failure writes can cross the failed fence.
    service.sessions.return_value.__aenter__.return_value.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_orphan_errors_block_before_original_or_media_access():
    service, _ = service_fixture()
    service.batch.return_value["manifest"]["orphan_entry_rows"] = [7]
    with pytest.raises(AppError) as error:
        await service.confirm("batch", "hash", 1, ["0"], [], [], "actor", "key")
    assert error.value.code == "IMPORT_BATCH_BLOCKED"
    service.original.assert_not_awaited()
    service.bind_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_binding_preserves_whole_word_chunk_and_image_roles():
    service, _ = service_fixture()
    raw = b"bytes"
    digest = hashlib.sha256(raw).hexdigest()
    item = {
        "resources": {
            "audio_path": {"path": "whole", "sha256": digest},
            "cover_path": {"path": "cover", "sha256": digest},
        },
        "entries": [
            {
                "entry_type": "word",
                "pronunciation_path": "word",
                "pronunciation_path_hash": digest,
                "image_path": "image",
                "image_path_hash": digest,
            },
            {
                "entry_type": "chunk",
                "pronunciation_path": "chunk",
                "pronunciation_path_hash": digest,
            },
        ],
    }
    await service.prepare(
        {"content_type": "dialogue", "created_by": "actor"},
        item,
        dict.fromkeys(["whole", "cover", "word", "image", "chunk"], raw),
    )
    assert {call.args[3] for call in service.bind_media.await_args_list} == {
        "WHOLE_AUDIO",
        "COVER_IMAGE",
        "WORD_AUDIO",
        "WORD_IMAGE",
        "CHUNK_AUDIO",
    }
    service.bind_media.reset_mock()
    await service.prepare(
        {"content_type": "short_reading", "created_by": "actor"},
        {"resources": {"audio_path": {"path": "whole", "sha256": digest}}, "entries": []},
        {"whole": raw},
    )
    assert service.bind_media.await_args.args[3] == "FULL_AUDIO"


@pytest.mark.asyncio
async def test_mysql_decimal_current_version_is_normalized_for_preview_json():
    import json
    from decimal import Decimal

    service, _ = service_fixture()
    mapping = Mock()
    mapping.mappings.return_value.first.return_value = {
        "content_id": "content",
        "input_hash": "hash",
        "version": 1,
    }
    content = Mock()
    content.mappings.return_value.first.return_value = {
        "series_id": "series",
        "current_version": Decimal("3"),
        "status": "DRAFT",
        "draft_revision_id": "revision",
    }
    session = AsyncMock()
    session.execute.side_effect = [mapping, content]
    # Use the actual method, rather than the fixture's mocked target lookup.
    result = await ImportService.current_mapping(service, session, "dialogue", "external")
    assert result["current_version"] == 3 and type(result["current_version"]) is int
    assert json.loads(json.dumps(result))["current_version"] == 3
