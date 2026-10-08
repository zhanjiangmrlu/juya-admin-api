import asyncio
import hashlib
import json
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
from juya_admin_api.modules.media.domain import AudioTarget
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
    # 功能:生成用于上传与解码检查的 PNG 测试图片字节。
    # 参数:无。
    # 返回:生成图片的 PNG 编码字节。
    stream = BytesIO()
    Image.new("RGB", (40, 40), "blue").save(stream, "PNG")
    return stream.getvalue()


class FakeOss:
    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        # 功能:模拟生成限定前缀、大小和有效期的 OSS 上传策略。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key_prefix: 上传策略授权的对象键前缀,限制可写目录。
        #     max_bytes: 允许读取或上传的字节数上限。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:模拟上传策略。
        return UploadPolicy("https://upload.test", object_key_prefix, max_bytes, expires_in, {})

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        # 功能:模拟封存媒体并返回固定命名空间内的测试对象键。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     data: 待读取或封存的媒体原始字节。
        #     asset_type: 媒体资源类别,例如 images 或 audio。
        #     content_type: 媒体 MIME 类型,供上传及封存元数据检查。
        # 返回:固定命名空间内的测试对象键。
        return (
            f"sealed/media/{asset_type}/fixture.png"
            if asset_type == "images"
            else "sealed/media/audio/fixture.wav"
        )

    async def head_object(self, object_key: str) -> ObjectMetadata:
        # 功能:返回测试对象的 MIME、大小和完整性等元数据。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:测试对象元数据。
        return ObjectMetadata(
            object_key,
            1024,
            "image/png",
            "a" * 64,
            {"decodable": "true", "security_status": "PASSED"},
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能:读取预设测试媒体字节,供服务端解码及限量读取检查。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     max_bytes: 允许读取或上传的字节数上限。
        # 返回:预设测试媒体的原始字节。
        return _image()

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        return f"https://signed.test/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        # 功能:模拟删除指定对象并记录清理操作。
        # 参数:
        #     self: 当前 FakeOss 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        del object_key


class LocalOcr:
    def __init__(self) -> None:
        # 功能:初始化 LocalOcr 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 LocalOcr 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.calls = 0

    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        # 功能:模拟 OCR 服务,返回预设结果或注入识别失败。
        # 参数:
        #     self: 当前 LocalOcr 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OCR 待识别图片的 OSS 对象键。
        #     template_type: OCR 模板类型,决定识别结果按对话或词汇等内容组织。
        # 返回:预设 OCR 识别结果。
        self.calls += 1
        return OcrResult(
            "ocr-request-1",
            f"recognized:{object_key}:{template_type}",
            [{"type": "title", "text": "Coffee time", "confidence": 0.98}],
        )


class LocalTts:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        # 功能:模拟 TTS 服务,返回预设音频结果或注入合成失败。
        # 参数:
        #     self: 当前 LocalTts 测试替身实例,保存本用例的预设状态或调用记录。
        #     audio_target: 待合成音频的业务目标标识。
        #     voice: TTS 合成使用的音色标识。
        #     text: TTS 待朗读文本或安全检查的文本内容。
        # 返回:预设 TTS 合成结果。
        return TtsResult(
            "tts-request-1",
            f"generated/{audio_target}/{voice}.mp3",
            len(text) * 100,
        )


class AuditRepository:
    def __init__(self) -> None:
        # 功能:初始化 AuditRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        # 功能:向测试仓库追加审计事件,供后续断言操作次数和内容。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     event: 待记录的审计或业务事件。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        json.dumps(event.before_summary)
        json.dumps(event.after_summary)
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能:从测试仓库返回最近的指定数量审计事件。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
        # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
        return self.events[-limit:]


class RecordingDispatcher:
    def __init__(self) -> None:
        # 功能:初始化 RecordingDispatcher 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 RecordingDispatcher 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.ocr_jobs: list[tuple[str, str, str]] = []
        self.tts_jobs: list[tuple[str, str, str, str, str]] = []

    async def enqueue_batch(self, batch_id: str) -> None:
        # 功能:记录需要派发的批任务标识。
        # 参数:
        #     self: 当前 RecordingDispatcher 测试替身实例,保存本用例的预设状态或调用记录。
        #     batch_id: 需要派发或操作的批任务标识。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        pass

    async def enqueue_ocr(self, job_id: str, object_key: str, template_type: str) -> None:
        # 功能:记录需要派发的 OCR 任务标识。
        # 参数:
        #     self: 当前 RecordingDispatcher 测试替身实例,保存本用例的预设状态或调用记录。
        #     job_id: 需要派发或操作的 OCR/TTS 工作任务标识。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     template_type: OCR 模板类型,决定识别结果按对话或词汇等内容组织。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.ocr_jobs.append((job_id, object_key, template_type))

    async def enqueue_tts(
        self,
        job_id: str,
        stable_key: str,
        target_type: str,
        text: str,
        voice: str,
    ) -> None:
        # 功能:记录需要派发的 TTS 任务标识。
        # 参数:
        #     self: 当前 RecordingDispatcher 测试替身实例,保存本用例的预设状态或调用记录。
        #     job_id: 需要派发或操作的 OCR/TTS 工作任务标识。
        #     stable_key: 新旧修订共用的稳定句子或条目键。
        #     target_type: 音频目标类别,区分整段音频与条目音频。
        #     text: TTS 待朗读文本或安全检查的文本内容。
        #     voice: TTS 合成使用的音色标识。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.tts_jobs.append((job_id, stable_key, target_type, text, voice))


def _client(
    *, whole_audio_target_id: str | None = None
) -> tuple[
    TestClient,
    MediaAdminService,
    InMemoryMediaAdminRepository,
    PersistentMediaTaskService,
    RecordingDispatcher,
    LocalOcr,
    AuditRepository,
]:

    # 功能:创建使用 HTTP 替身传输的小程序内部接口客户端。
    # 参数:
    #     whole_audio_target_id: 场景整段音频的目标标识。
    # 返回:tuple[TestClient, MediaAdminService, InMemoryMediaAdminRepository, PersistentMediaTas
    #       kService, RecordingDispatcher, LocalOcr, AuditRepository],由本用例预设的数据或所组装
    #       的测试资源构成。
    async def read_admin(x_test_admin: str | None = Header(default=None)) -> SessionRecord:
        # 功能:检查测试认证头并返回预设管理员会话。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        # 返回:测试管理员会话。
        if x_test_admin is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return SessionRecord("session", 7, "token", "csrf", "test device", NOW, NOW)

    async def write_admin(
        x_test_admin: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> SessionRecord:
        # 功能:在测试认证通过后检查写请求 CSRF 令牌。
        # 参数:
        #     x_test_admin: 测试专用管理员认证请求头,用于替代真实登录会话。
        #     x_csrf_token: 写操作请求的 CSRF 令牌,与当前测试会话的预设值比较。
        # 返回:测试管理员会话。
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
        content={
            "title_en": "Manual draft",
            "original_image_asset_id": "asset-image-1",
            "audio": {"target_id": whole_audio_target_id},
        },
        created_by="7",
        created_at=NOW,
    )
    content = ContentService(content_repository)
    repository = InMemoryMediaAdminRepository()
    repository.register_draft("scene-1", "draft-1")
    admin_service = MediaAdminService(repository)
    ocr = LocalOcr()
    # 匿名函数: 从合成结果提取对象键并注册稳定测试资源。
    # 参数:
    #     result: TTS 合成结果, 从 object_key 提取生成音频对象。
    #     now: 注册资源时使用的测试时间。
    # 返回: 资源注册回调产生的可等待对象, 等待后得到测试资源标识。
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
                "sealed/media/images/fixture.png",
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
                "sealed/media/audio/fixture.wav",
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
    worker._media_service = MediaService(FakeOss(), assets)
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
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
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
    # 功能:将生成音频对象键映射为可重复的测试资源标识。
    # 参数:
    #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
    #     now: 测试指定的当前时间,用于稳定计算期限、状态迁移和事件时间。
    # 返回:字符串。
    del now
    return f"asset:{object_key}"


def test_false_template_is_rejected_before_quota_and_dispatch() -> None:
    # 功能:验证无效模板在消耗额度和派发任务前被拒绝。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _admin, repository, _worker, dispatcher, ocr, _audit = _client()
    response = client.post(
        "/api/v1/admin/media/ocr/jobs",
        json={
            "asset_id": "asset-image-1",
            "scene_id": "scene-1",
            "revision_id": "draft-1",
            "series_id": "series-1",
            "template_id": "fabricated-template",
        },
        headers={"X-Test-Admin": "1", "X-CSRF-Token": "csrf", "X-Idempotency-Key": "bad-template"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "OCR_TEMPLATE_MISMATCH"
    assert not dispatcher.ocr_jobs and ocr.calls == 0
    assert not repository.jobs


def test_audio_targets_are_scoped_to_scene_and_saved_entries() -> None:
    # 功能:验证音频目标限定于场景及已保存的条目。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, admin, _repository, _worker, _dispatcher, _ocr, _audit = _client()
    own = asyncio.run(admin.create_audio_target("scene-1", "scene"))
    asyncio.run(admin.create_audio_target("other-scene", "scene"))
    asyncio.run(admin.create_audio_target("unrelated-word", "vocabulary"))
    response = client.get(
        "/api/v1/admin/media/audio-targets?scene_id=scene-1", headers={"X-Test-Admin": "1"}
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [own.id]


def test_audio_targets_include_legacy_whole_target_referenced_by_revision() -> None:
    # 功能:验证音频目标列表包含修订引用的旧版整段目标。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _admin, repository, *_ = _client(whole_audio_target_id="legacy-target")
    repository.audio_targets["legacy-target"] = AudioTarget(
        "legacy-target", "scene:scene-1", "scene", None
    )
    repository.audio_targets["foreign-target"] = AudioTarget(
        "foreign-target", "scene:other-scene", "scene", None
    )
    response = client.get(
        "/api/v1/admin/media/audio-targets?scene_id=scene-1", headers={"X-Test-Admin": "1"}
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["legacy-target"]


def test_ocr_settings_require_key_and_write_json_serializable_audit() -> None:
    # 功能:验证 OCR 配置要求幂等键且审计可序列化为 JSON。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _admin, _repository, _worker, dispatcher, ocr, audit = _client()
    payload = {
        "enabled": True,
        "monthly_limit": 0,
        "free_quota": 1000,
        "paid_disabled": True,
        "verify_quota": True,
    }
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    assert (
        client.put("/api/v1/admin/media/ocr/settings", json=payload, headers=headers).status_code
        == 422
    )
    response = client.put(
        "/api/v1/admin/media/ocr/settings",
        json=payload,
        headers=headers | {"X-Idempotency-Key": "settings-save-1"},
    )
    assert response.status_code == 200
    assert response.json()["remaining"] == 0
    event = audit.events[-1]
    assert event.action == "media.ocr.settings"
    assert event.after_summary["quota_verified_at"] == NOW.isoformat()
    assert event.after_summary["updated_at"] == NOW.isoformat()
    assert dispatcher.ocr_jobs == [] and ocr.calls == 0


def test_ocr_http_worker_confirmation_and_redelivery_flow() -> None:
    # 功能:验证 OCR 的 HTTP、工作任务、确认及重复投递流程。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _admin, repository, worker, dispatcher, ocr, _audit = _client()
    assert client.get("/api/v1/admin/media/ocr/jobs/missing").status_code == 401
    payload = {
        "asset_id": "asset-image-1",
        "object_key": "sealed/media/images/fixture.png",
        "scene_id": "scene-1",
        "revision_id": "draft-1",
        "series_id": "series-1",
        "template_id": "dialogue",
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
    # 功能:验证取消音频批次与回收站命令保留已经完成的状态。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
            "object_key": "sealed/media/images/fixture.png",
            "scene_id": "scene-1",
            "revision_id": "draft-1",
            "series_id": "series-1",
            "template_id": "dialogue",
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
    # 功能:验证元数据预览仅管理员可用且 TTS 预览不派发任务。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
