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
        self.sign_count = 0

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        raise AssertionError("not used")

    async def head_object(self, object_key: str) -> ObjectMetadata:
        raise AssertionError("not used")

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        self.sign_count += 1
        return f"https://signed.example/{self.sign_count}?expires={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        raise AssertionError("not used")


class AuditRepository:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def list_recent(self, limit: int) -> list[AuditEvent]:
        return self.events[-limit:]


def _client() -> tuple[
    TestClient,
    InMemoryFeedbackRepository,
    FakeOssProvider,
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
