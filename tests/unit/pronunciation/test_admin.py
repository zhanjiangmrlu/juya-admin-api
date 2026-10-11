from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.modules.pronunciation.admin_router import create_pronunciation_admin_router
from juya_admin_api.modules.pronunciation.admin_store import PronunciationAdminStore
from juya_admin_api.modules.pronunciation.domain import Config
from juya_admin_api.shared.errors import AppError


def store_with(*rows):
    session = AsyncMock()
    results = []
    for row in rows:
        result = Mock()
        result.mappings.return_value.first.return_value = row
        results.append(result)
    session.execute.side_effect = results
    session.begin = Mock(return_value=AsyncMock())
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    store = PronunciationAdminStore(
        sessions, Config(), sign_asset=AsyncMock(return_value="signed"), verify_asset=AsyncMock()
    )
    return store, session


@pytest.mark.asyncio
async def test_preview_binds_uploaded_word_version_and_verifies_before_signing():
    store, _ = store_with(
        {
            "public_id": "pronunciation",
            "status": "CONFIRMED",
            "target_type": "WORD",
            "uploaded_target_id": "word-target",
            "uploaded_resource_version": "fixed",
        },
        {
            "asset_id": "asset",
            "asset_status": "CONFIRMED",
            "security_status": "PASSED",
            "sha256": "a" * 64,
            "object_key": "sealed",
            "duration_ms": 40,
            "mime_type": "audio/mpeg",
            "size_bytes": 50,
            "audio_status": "ACTIVE",
            "target_type": "WORD",
        },
    )
    result = await store.preview("pronunciation", "fixed")
    assert result["source"] == "UPLOADED" and result["resource_version"] == "fixed"
    assert result["expires_in"] == 60 and result["url"] == "signed"
    store.verify_asset.assert_awaited_once_with("asset")
    store.sign_asset.assert_awaited_once_with("asset", 60)
    assert "object_key" not in result


@pytest.mark.asyncio
async def test_uploaded_whole_audio_and_wrong_version_cannot_be_signed():
    for version, audio_type in (("other", "WORD"), ("fixed", "WHOLE")):
        store, _ = store_with(
            {
                "public_id": "p",
                "status": "CONFIRMED",
                "target_type": "WORD",
                "uploaded_target_id": "t",
                "uploaded_resource_version": "fixed",
            },
            {"target_type": audio_type},
        )
        with pytest.raises(AppError):
            await store.preview("p", version)
        store.sign_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_actual_head_prevents_url_and_unknown_cannot_retry():
    store, _ = store_with({"status": "UNKNOWN", "audio": b"retained", "lease_until": 0})
    with pytest.raises(AppError) as exc:
        await store.retry("job")
    assert exc.value.code == "PRONUNCIATION_RETRY_UNSAFE"
    store.sign_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_recovery_requeues_retained_audio_without_reserving_cloud_budget():
    store, session = store_with({"status": "RECOVER", "audio": b"retained", "lease_until": 0}, {})
    result = await store.retry("job")
    assert result == {"task_id": "job", "status": "RECOVER", "paid_retry": False}
    sql = str(session.execute.call_args_list[-1].args[0])
    assert "outbox_pending" in sql and "cloud_usage_attempt" not in sql


@pytest.mark.asyncio
async def test_cache_preview_head_failure_never_signs():
    from dataclasses import asdict

    from juya_admin_api.modules.pronunciation.domain import Target

    store, _ = store_with(
        asdict(Target("target", "hello")),
        {
            "asset_id": "asset",
            "asset_status": "CONFIRMED",
            "security_status": "PASSED",
            "sha256": "a" * 64,
            "object_key": "sealed",
            "duration_ms": 40,
            "mime_type": "audio/mpeg",
            "size_bytes": 50,
        },
    )
    store.verify_asset.side_effect = AppError("ASSET_MISSING", "固定对象不存在", 409)
    with pytest.raises(AppError):
        await store.preview("target", "fixed-cache-version")
    store.sign_asset.assert_not_awaited()


def test_admin_router_requires_session_and_fixed_preview_body_no_arbitrary_text():
    from fastapi import HTTPException

    def read_admin():
        return "admin"

    def write_admin():
        raise HTTPException(403)

    store = Mock()
    store.preview = AsyncMock(return_value={"url": "signed", "expires_in": 60})
    app = FastAPI()
    app.include_router(
        create_pronunciation_admin_router(
            store, current_admin=read_admin, current_admin_write=write_admin
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/admin/pronunciations/targets/target/preview", json={"resource_version": "version"}
    )
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert (
        client.post(
            "/api/v1/admin/pronunciations/targets/target/preview",
            json={"resource_version": "v", "text": "arbitrary"},
        ).status_code
        == 422
    )
    assert client.post("/api/v1/admin/pronunciations/tasks/job/retry").status_code == 403
