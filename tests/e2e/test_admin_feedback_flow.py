from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.modules.feedback.domain import (
    FeedbackScreenshot,
    FeedbackTicket,
    FeedbackTimelineEvent,
)
from juya_admin_api.modules.feedback.repository import InMemoryFeedbackRepository
from juya_admin_api.modules.feedback.router import create_admin_feedback_router
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError, install_error_handlers

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


class FakeOssProvider:
    def __init__(self) -> None:
        # 功能:初始化 FakeOssProvider 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.sign_count = 0

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        # 功能:模拟生成限定前缀、大小和有效期的 OSS 上传策略。
        # 参数:
        #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key_prefix: 上传策略授权的对象键前缀,限制可写目录。
        #     max_bytes: 允许读取或上传的字节数上限。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise AssertionError("not used")

    async def head_object(self, object_key: str) -> ObjectMetadata:
        # 功能:返回测试对象的 MIME、大小和完整性等元数据。
        # 参数:
        #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise AssertionError("not used")

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        # 功能:模拟对象下载签名并保留有效期供断言。
        # 参数:
        #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        #     expires_in: 签名 URL 或上传策略有效期,单位为秒。
        # 返回:预设资源签名 URL 字符串。
        self.sign_count += 1
        return f"https://signed.example/{self.sign_count}?expires={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        # 功能:模拟删除指定对象并记录清理操作。
        # 参数:
        #     self: 当前 FakeOssProvider 测试替身实例,保存本用例的预设状态或调用记录。
        #     object_key: OSS 桶内对象键,指定要读取、签名、审核或删除的测试资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise AssertionError("not used")


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
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        # 功能:从测试仓库返回最近的指定数量审计事件。
        # 参数:
        #     self: 当前 AuditRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     limit: 最多返回的记录数,用于最近审计或任务批量处理。
        # 返回:list[AuditEvent],由本用例预设的数据或所组装的测试资源构成。
        return self.events[-limit:]


def _client() -> tuple[
    TestClient,
    InMemoryFeedbackRepository,
    FakeOssProvider,
    AuditRepository,
]:

    # 功能:创建使用 HTTP 替身传输的小程序内部接口客户端。
    # 参数:无。
    # 返回:tuple[TestClient, InMemoryFeedbackRepository, FakeOssProvider, AuditRepository],由本
    #       用例预设的数据或所组装的测试资源构成。
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

    repository = InMemoryFeedbackRepository()
    ticket = FeedbackTicket(
        id="FB-1",
        user_id="USER-1",
        category="CONTENT",
        description="<b>private feedback body</b>",
        source={"app_version": "1.0.0"},
        status="PROCESSING",
        sla_hours=48,
        deadline_at=NOW + timedelta(hours=48),
        sla_remaining_seconds=None,
        supplement_rounds=0,
        reopen_count=0,
        created_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )
    repository.tickets[ticket.id] = ticket
    repository.screenshots[ticket.id] = FeedbackScreenshot(ticket.id, "feedback/private/shot.png")
    repository.rounds[ticket.id] = []
    repository.replies[ticket.id] = []
    repository.internal_notes[ticket.id] = []
    repository.timeline.extend(
        [
            FeedbackTimelineEvent(ticket.id, "CREATED", "USER", ticket.user_id, ticket.created_at),
            FeedbackTimelineEvent(ticket.id, "PROCESSING_STARTED", "ADMIN", "7", ticket.updated_at),
        ]
    )
    oss = FakeOssProvider()
    media = MediaService(oss, InMemoryMediaRepository(), signed_url_ttl_seconds=300)
    audit_repository = AuditRepository()
    app = FastAPI()
    install_error_handlers(app)
    # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
    # 参数: 无。
    # 返回: 对应测试时间或 UTC 当前时间。
    app.include_router(
        create_admin_feedback_router(
            FeedbackService(repository),
            media_service=media,
            audit_service=AuditService(audit_repository),
            current_admin=read_admin,
            current_admin_write=write_admin,
            clock=lambda: NOW,
        )
    )
    return TestClient(app), repository, oss, audit_repository


def test_feedback_reads_require_session_return_aggregate_and_disable_storage() -> None:
    # 功能:验证反馈读取要求登录、返回聚合数据且禁用存储缓存。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _repository, _oss, _audit = _client()

    assert client.get("/api/v1/admin/feedback").status_code == 401
    headers = {"X-Test-Admin": "1"}
    page = client.get(
        "/api/v1/admin/feedback?status=PROCESSING&category=CONTENT"
        "&keyword=USER-1&sla=ON_TRACK&page=1&page_size=20",
        headers=headers,
    )
    detail = client.get("/api/v1/admin/feedback/FB-1", headers=headers)

    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["sla_state"] == "ON_TRACK"
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert detail.json()["description"] == "<b>private feedback body</b>"
    assert [item["event_type"] for item in detail.json()["timeline"]] == [
        "CREATED",
        "PROCESSING_STARTED",
    ]
    assert detail.json()["screenshots"] == [
        {
            "security_status": "PASSED",
            "delete_after": None,
            "deleted_at": None,
        }
    ]
    assert "url" not in str(detail.json()).lower()


def test_screenshot_url_requires_csrf_is_short_lived_and_is_never_reused() -> None:
    # 功能:验证截图签名要求 CSRF、有效期短且不会复用。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, _repository, oss, audit = _client()
    path = "/api/v1/admin/feedback/FB-1/screenshot-url"
    assert client.post(path, headers={"X-Test-Admin": "1"}).status_code == 403
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}

    first = client.post(path, headers=headers)
    second = client.post(path, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.headers["Cache-Control"] == second.headers["Cache-Control"] == "no-store"
    assert first.json()["expires_at"] == "2026-09-29T08:05:00Z"
    assert first.json()["url"] != second.json()["url"]
    assert oss.sign_count == 2
    serialized_audit = repr(audit.events)
    assert "https://signed.example" not in serialized_audit
    assert "feedback/private/shot.png" not in serialized_audit
    assert "private feedback body" not in serialized_audit


def test_internal_note_requires_csrf_and_idempotency_and_never_leaks_into_audit() -> None:
    # 功能:验证内部备注要求 CSRF 和幂等键且内容不会进入审计。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository, _oss, audit = _client()
    path = "/api/v1/admin/feedback/FB-1/internal-notes"
    headers = {"X-Test-Admin": "1", "X-CSRF-Token": "csrf"}
    assert client.post(path, json={"content": "secret"}, headers=headers).status_code == 422
    headers["X-Idempotency-Key"] = "note-1"

    created = client.post(path, json={"content": "secret investigation"}, headers=headers)
    replay = client.post(path, json={"content": "changed content"}, headers=headers)
    detail = client.get("/api/v1/admin/feedback/FB-1", headers={"X-Test-Admin": "1"})

    assert created.status_code == replay.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    assert replay.json() == created.json()
    assert [item["content"] for item in detail.json()["internal_notes"]] == ["secret investigation"]
    assert len(repository.internal_notes["FB-1"]) == 1
    assert "secret investigation" not in repr(audit.events)


def test_closed_ticket_rejects_new_command_without_duplicate_event() -> None:
    # 功能:验证已关闭反馈拒绝新命令且不重复产生事件。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    client, repository, _oss, _audit = _client()
    path = "/api/v1/admin/feedback/FB-1/commands/close-insufficient"
    headers = {
        "X-Test-Admin": "1",
        "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "close-1",
    }
    first = client.post(path, json={"reason": "信息不足"}, headers=headers)
    headers["X-Idempotency-Key"] = "close-2"
    conflict = client.post(path, json={"reason": "再次关闭"}, headers=headers)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "FEEDBACK_STATE_CONFLICT"
    assert (
        len([item for item in repository.timeline if item.event_type == "CLOSED_INSUFFICIENT"]) == 1
    )
