import asyncio
import hashlib
from datetime import UTC, datetime
from io import BytesIO

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from PIL import Image

from juya_admin_api.integrations.content_security.aliyun import LocalFixtureContentSecurityProvider
from juya_admin_api.integrations.ocr.protocol import OcrResult
from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.integrations.tts.protocol import TtsResult
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.quota import InMemoryOcrQuotaRepository, OcrQuotaService
from juya_admin_api.modules.media.router import create_media_router
from juya_admin_api.modules.media.service import (
    InMemoryMediaAdminRepository,
    InMemoryMediaRepository,
    MediaAdminService,
    MediaAsset,
    MediaService,
)
from juya_admin_api.modules.media.tasks import PersistentMediaTaskService
from juya_admin_api.shared.errors import AppError, install_error_handlers

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def _image() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (40, 40), "blue").save(stream, "PNG")
    return stream.getvalue()


class FakeOss:
    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        return UploadPolicy("https://upload.test", object_key_prefix, max_bytes, expires_in, {})

    async def head_object(self, object_key: str) -> ObjectMetadata:
        return ObjectMetadata(
            object_key,
            1024,
            "image/png",
            "a" * 64,
            {"decodable": "true", "security_status": "PASSED"},
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        return _image()

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        return f"https://signed.test/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        del object_key


class LocalOcr:
    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        self.calls += 1
        return OcrResult(
            "ocr-request-1",
            f"recognized:{object_key}:{template_type}",
            [{"type": "title", "text": "Coffee time", "confidence": 0.98}],
        )


class LocalTts:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        return TtsResult(
            "tts-request-1",
            f"generated/{audio_target}/{voice}.mp3",
            len(text) * 100,
        )


class AuditRepository:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        return self.events[-limit:]


class RecordingDispatcher:
    def __init__(self) -> None:
        self.ocr_jobs: list[tuple[str, str, str]] = []
        self.tts_jobs: list[tuple[str, str, str, str, str]] = []

    async def enqueue_batch(self, batch_id: str) -> None:
        pass

    async def enqueue_ocr(self, job_id: str, object_key: str, template_type: str) -> None:
        self.ocr_jobs.append((job_id, object_key, template_type))

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None:
        self.tts_jobs.append((job_id, stable_key, target_type, text, voice))


def _client() -> tuple[
    TestClient,
    MediaAdminService,
    InMemoryMediaAdminRepository,
    PersistentMediaTaskService,
    RecordingDispatcher,
    LocalOcr,
    AuditRepository,
]:
    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return SessionRecord("session", 7, "token", "csrf", "test device", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        admin = await read_admin(x_test_admin)
        if x_csrf_token != "csrf":
            raise AppError("ADMIN_CSRF_INVALID", "CSRF token 无效", 403)
        return admin

    content_repository = InMemoryContentRepository()
    content_repository.scenes["scene-1"] = Scene(
        "scene-1",
        "series-1",
        title="Coffee",
        status="DRAFT",
        draft_revision_id="draft-1",
        updated_at=NOW,
    )
    content_repository.revisions["draft-1"] = SceneRevision(
        "draft-1",
        "scene-1",
        None,
        version=1,
        status="DRAFT",
        content={"title_en": "Manual draft", "original_image_asset_id": "asset-image-1"},
        created_by="7",
        created_at=NOW,
    )
    content = ContentService(content_repository)
    repository = InMemoryMediaAdminRepository()
    repository.register_draft("scene-1", "draft-1")
    admin_service = MediaAdminService(repository)
    ocr = LocalOcr()
    worker = PersistentMediaTaskService(
        admin_service,
        repository,
        ocr,
        LocalTts(),
        register_generated_audio=lambda result, now: _generated_asset(result.object_key, now),
    )
    assets = InMemoryMediaRepository()
    asyncio.run(
        assets.save(
            MediaAsset(
                "asset-image-1",
                "uploads/images/7/fixtures/card.png",
                "images",
                "image/png",
                len(_image()),
                hashlib.sha256(_image()).hexdigest(),
                "CONFIRMED",
                "PASSED",
                "7",
                NOW,
                width=40,
                height=40,
            )
        )
    )
    asyncio.run(
        assets.save(
            MediaAsset(
                "asset-audio-2",
                "uploads/audio/7/fixtures/whole.wav",
                "audio",
                "audio/wav",
                1024,
                "b" * 64,
                "CONFIRMED",
                "PASSED",
                "7",
                NOW,
                duration_ms=1200,
            )
        )
    )
    quota = OcrQuotaService(InMemoryOcrQuotaRepository())
    asyncio.run(
        quota.configure(
            enabled=True,
            monthly_limit=5,
            free_quota=5,
            paid_disabled=True,
            verify_quota=True,
            actor_id="7",
            now=NOW,
        )
    )
    dispatcher = RecordingDispatcher()
    audit_repository = AuditRepository()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_media_router(
            MediaService(
                FakeOss(),
                assets,
                security=LocalFixtureContentSecurityProvider("test", explicitly_enabled=True),
            ),
            ocr_quota_service=quota,
            admin_service=admin_service,
            content_service=content,
            audit_service=AuditService(audit_repository),
            task_dispatcher=dispatcher,
            current_admin=read_admin,
            current_admin_write=write_admin,
            clock=lambda: NOW,
        )
    )
    return (
        TestClient(app),
        admin_service,
        repository,
        worker,
        dispatcher,
        ocr,
        audit_repository,
    )


async def _generated_asset(object_key: str, now: datetime) -> str:
    del now
    return f"asset:{object_key}"


def test_ocr_http_worker_confirmation_and_redelivery_flow() -> None:
    client, _admin, repository, worker, dispatcher, ocr, _audit = _client()
    assert client.get("/api/v1/admin/media/ocr/jobs/missing").status_code == 401
    payload = {
        "asset_id": "asset-image-1",
        "object_key": "uploads/images/7/fixtures/card.png",
        "scene_id": "scene-1",
        "revision_id": "draft-1",
        "series_id": "series-1",
        "template_id": "learning-card",
    }
    assert (
        client.post(
            "/api/v1/admin/media/ocr/jobs",
            json=payload,
            headers={"X-Test-Admin": "1", "X-Idempotency-Key": "ocr-1"},
        ).status_code
        == 403
    )
    headers = {
        "X-Test-Admin": "1",
        "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "ocr-1",
    }
    created = client.post("/api/v1/admin/media/ocr/jobs", json=payload, headers=headers)
    replayed = client.post("/api/v1/admin/media/ocr/jobs", json=payload, headers=headers)
    assert created.status_code == 201
    assert replayed.json()["id"] == created.json()["id"]
    assert len(dispatcher.ocr_jobs) == 2

    job_id, object_key, template_type = dispatcher.ocr_jobs[0]
    asyncio.run(worker.run_ocr(job_id, object_key, template_type, NOW))
    asyncio.run(worker.run_ocr(job_id, object_key, template_type, NOW))
    status = client.get(f"/api/v1/admin/media/ocr/jobs/{job_id}", headers={"X-Test-Admin": "1"})
    candidate = client.get(
        f"/api/v1/admin/media/ocr/jobs/{job_id}/candidate",
        headers={"X-Test-Admin": "1"},
    )
    assert status.json()["status"] == "SUCCEEDED"
    assert candidate.json()["structured_candidate"]["blocks"][0]["text"] == "Coffee time"
    assert ocr.calls == 1
    assert len(repository.ocr_candidates) == 1

    confirmed = client.post(
        f"/api/v1/admin/media/ocr/jobs/{job_id}/commands/confirm",
        json={"scene_id": "scene-1", "content": {"title": "Reviewed coffee"}},
        headers=headers | {"X-Idempotency-Key": "confirm-1"},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["code"] == "OCR_ADOPTION_REQUIRED"
    assert repository.ocr_candidates[job_id].confirmed_revision_id is None


def test_cancel_audio_batch_and_trash_commands_preserve_completed_state() -> None:
    client, admin, _repository, worker, dispatcher, ocr, audit = _client()
    headers = {
        "X-Test-Admin": "1",
        "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "command-1",
    }
    created = client.post(
        "/api/v1/admin/media/ocr/jobs",
        json={
            "asset_id": "asset-image-1",
            "object_key": "uploads/images/7/fixtures/card.png",
            "scene_id": "scene-1",
            "revision_id": "draft-1",
            "series_id": "series-1",
            "template_id": "learning-card",
        },
        headers=headers,
    ).json()
    cancelled = client.post(
        f"/api/v1/admin/media/ocr/jobs/{created['id']}/commands/cancel",
        json={},
        headers=headers,
    )
    job_id, object_key, template_type = dispatcher.ocr_jobs[-1]
    asyncio.run(worker.run_ocr(job_id, object_key, template_type, NOW))
    assert cancelled.json()["status"] == "CANCELLED"
    assert ocr.calls == 0

    manual = asyncio.run(
        admin.create_audio_candidate(
            stable_key="sentence-1",
            target_type="SENTENCE",
            asset_id="asset-audio-1",
            source="MANUAL",
            actor_id="7",
            now=NOW,
        )
    )
    target = asyncio.run(admin.confirm_audio_version(manual.id, actor_id="7", now=NOW))
    uploaded = client.post(
        f"/api/v1/admin/media/audio-targets/{target.id}/versions",
        json={"asset_id": "asset-audio-2"},
        headers=headers,
    )
    replayed_upload = client.post(
        f"/api/v1/admin/media/audio-targets/{target.id}/versions",
        json={"asset_id": "asset-audio-2"},
        headers=headers,
    )
    assert uploaded.status_code == 201
    assert replayed_upload.json()["id"] == uploaded.json()["id"]
    confirmed = client.post(
        f"/api/v1/admin/media/audio-versions/{uploaded.json()['id']}/commands/confirm",
        json={},
        headers=headers,
    )
    assert confirmed.json()["active_version_id"] == uploaded.json()["id"]
    rolled_back = client.post(
        f"/api/v1/admin/media/audio-targets/{target.id}/commands/rollback",
        json={"version_id": manual.id},
        headers=headers,
    )
    assert rolled_back.json()["active_version_id"] == manual.id

    batch = client.post(
        "/api/v1/admin/media/batch-jobs",
        json={"job_type": "VALIDATE", "target_ids": ["scene-1", "scene-2"]},
        headers=headers | {"X-Idempotency-Key": "batch-1"},
    )
    assert batch.status_code == 201
    batch_id = batch.json()["id"]
    cancelled_batch = client.post(
        f"/api/v1/admin/media/batch-jobs/{batch_id}/commands/cancel",
        json={},
        headers=headers,
    )
    assert {item["status"] for item in cancelled_batch.json()["items"]} == {"CANCELLED"}

    trashed = client.post(
        "/api/v1/admin/media/trash",
        json={"scene_id": "scene-1", "revision_id": "draft-1"},
        headers=headers | {"X-Idempotency-Key": "trash-1"},
    )
    assert trashed.status_code == 201
    restored = client.post(
        f"/api/v1/admin/media/trash/{trashed.json()['id']}/commands/restore",
        json={},
        headers=headers,
    )
    assert restored.json()["status"] == "RESTORED"
    assert "media.audio.rollback" in [event.action for event in audit.events]
    assert "media.trash.restore" in [event.action for event in audit.events]


def test_metadata_preview_is_admin_only_and_tts_request_does_not_dispatch() -> None:
    client, _admin, _repository, _worker, dispatcher, _ocr, _audit = _client()
    assert client.get("/api/v1/admin/media/assets/asset-image-1").status_code == 401
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf", "X-Idempotency-Key": "target-new"}
    result = client.get("/api/v1/admin/media/assets/asset-image-1", headers=headers)
    assert (result.json()["width"], result.json()["height"]) == (40, 40)
    assert client.get("/api/v1/admin/media/assets/asset-image-1/signed-url").status_code == 401
    preview = client.get("/api/v1/admin/media/assets/asset-image-1/signed-url", headers=headers)
    assert preview.status_code == 200 and preview.json()["url"].startswith("https://signed.test/")
    target = client.post(
        "/api/v1/admin/media/audio-targets",
        headers=headers,
        json={"stable_key": "scene:scene-1", "target_type": "scene"},
    )
    assert target.status_code == 201
    disabled = client.post(
        f"/api/v1/admin/media/audio-targets/{target.json()['id']}/commands/generate",
        headers=headers,
        json={
            "stable_key": "scene:scene-1",
            "target_type": "scene",
            "text": "hello",
            "voice": "voice",
        },
    )
    assert disabled.status_code == 409 and disabled.json()["code"] == "TTS_DISABLED"
    assert dispatcher.tts_jobs == []
