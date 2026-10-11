from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.modules.content.schemas import (
    AudioReference,
    DialogueSentence,
    SceneContent,
    SceneEntry,
)
from juya_admin_api.modules.content.v15_materials import (
    apply_bindings,
    material_manifest,
    validate_bindings,
)
from juya_admin_api.modules.content.v15_materials_router import create_materials_router
from juya_admin_api.modules.short_reading.schemas import ReadingContent


def card():
    return SceneContent(
        schema_version=2,
        template_type="word_card",
        vocabulary=[
            SceneEntry(
                entry_id="river",
                english="bank",
                chinese="河岸",
                sense_id="sense-river",
                source_id="source-river",
            ),
            SceneEntry(
                entry_id="finance",
                english="bank",
                chinese="银行",
                sense_id="sense-finance",
                source_id="source-finance",
            ),
        ],
        chunks=[SceneEntry(entry_id="chunk", english="by the bank", chinese="在岸边")],
    )


def test_duplicate_normalized_title_is_only_a_manual_candidate_not_auto_selected():
    targets = [
        {"revision_id": "one", "title_en": "A BANK"},
        {"revision_id": "two", "title_en": "a bank"},
    ]
    files = material_manifest({"images/\uff21 Bank.png": b"image"}, targets, "word_card")
    assert files[0]["ambiguous_title"] and files[0]["requires_manual_selection"]
    assert [row["revision_id"] for row in files[0]["matching_candidates"]] == ["one", "two"]
    assert files[0]["sha256"] and files[0]["filename"] == "\uff21 Bank.png"
    assert "selected_revision_id" not in files[0]


def test_word_image_selects_server_entry_id_without_spelling_or_meaning_merge():
    before = card()
    after = apply_bindings(
        before,
        [
            {
                "path": "word.png",
                "role": "WORD_IMAGE",
                "entry_id": "finance",
                "media": {"asset_id": "image"},
            }
        ],
    )
    assert after.vocabulary[0].icon_asset_id is None
    selected = after.vocabulary[1]
    assert selected.icon_asset_id == "image"
    assert (selected.entry_id, selected.sense_id, selected.source_id, selected.chinese) == (
        "finance",
        "sense-finance",
        "source-finance",
        "银行",
    )
    assert before.vocabulary[1].icon_asset_id is None


@pytest.mark.parametrize(
    "role,entry",
    [
        ("WORD_IMAGE", "chunk"),
        ("WORD_AUDIO", "chunk"),
        ("CHUNK_AUDIO", "finance"),
        ("CHUNK_IMAGE", "chunk"),
        ("WORD_IMAGE", "missing"),
        ("COVER_IMAGE", "finance"),
    ],
)
def test_invalid_role_and_stable_entry_identity_are_rejected(role, entry):
    from juya_admin_api.shared.errors import AppError

    with pytest.raises(AppError):
        validate_bindings(card(), [{"path": "x", "role": role, "entry_id": entry}])


def test_whole_audio_replacement_revokes_old_timing_and_slice_evidence():
    before = SceneContent(
        audio=AudioReference(target_id="whole", version_id="old", asset_id="old", duration_ms=1000),
        dialogue=[
            DialogueSentence(
                id="line",
                start_ms=0,
                end_ms=1000,
                timing_confirmed=True,
                boundary_verified_by="admin",
                slice_target_id="slice",
                slice_version="v1",
                slice_dependency_hash="hash",
            )
        ],
    )
    after = apply_bindings(
        before,
        [
            {
                "path": "full.wav",
                "role": "WHOLE_AUDIO",
                "media": {
                    "target_id": "new",
                    "version_id": "new",
                    "asset_id": "new",
                    "duration_ms": 2000,
                },
            }
        ],
    )
    line = after.dialogue[0]
    assert not line.timing_confirmed and not line.boundary_verified_by
    assert (
        line.start_ms is line.end_ms is line.slice_target_id is line.slice_dependency_hash is None
    )


def test_reading_full_audio_has_no_entries_and_requires_fresh_human_listening():
    after = apply_bindings(
        ReadingContent(),
        [
            {
                "path": "full.wav",
                "role": "FULL_AUDIO",
                "media": {
                    "target_id": "new",
                    "version_id": "new",
                    "asset_id": "new",
                    "duration_ms": 2000,
                    "listening_confirmed": True,
                    "confirmed_asset_id": "new",
                    "confirmed_version_id": "new",
                },
            }
        ],
    )
    assert not after.audio.listening_confirmed and after.audio.confirmed_asset_id is None
    assert material_manifest({"cover.png": b"x", "full.wav": b"y"}, [], "short_reading")[0][
        "allowed_roles"
    ] == ["COVER_IMAGE"]
    from juya_admin_api.shared.errors import AppError

    with pytest.raises(AppError):
        validate_bindings(after, [{"path": "x", "role": "WORD_IMAGE", "entry_id": "word"}])


def test_material_bind_route_is_202_and_forbids_arbitrary_media_or_english_identity():
    service = AsyncMock()
    service.queue_bind.return_value = {"stage_id": "stage"}

    async def admin():
        return SimpleNamespace(admin_user_id=7)

    app = FastAPI()
    app.include_router(
        create_materials_router(service, current_admin=admin, current_admin_write=admin)
    )
    payload = {
        "revision_id": "rev",
        "expected_version": 1,
        "manual_checked": True,
        "bindings": [{"path": "bank.png", "role": "WORD_IMAGE", "entry_id": "stable"}],
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/v1/admin/content-materials/batch/bindings",
                json=payload,
                headers={"Idempotency-Key": "key"},
            ).status_code
            == 202
        )
        payload["bindings"][0]["english"] = "bank"
        assert (
            client.post(
                "/api/v1/admin/content-materials/batch/bindings",
                json=payload,
                headers={"Idempotency-Key": "key"},
            ).status_code
            == 422
        )
    service.queue_bind.assert_awaited_once()
