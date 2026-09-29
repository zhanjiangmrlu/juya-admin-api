import json
from datetime import UTC, datetime

import httpx
import pytest

from juya_admin_api.infrastructure.security.service_hmac import sign_request
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


def _contact(user_id: str = "user-1") -> dict[str, object]:
    return {
        "user_id": user_id,
        "wechat_id": "wx-private",
        "contact_status": "PENDING",
        "change_pending": True,
        "verified_at": None,
        "verified_by": None,
        "updated_at": "2026-09-29T00:00:00Z",
    }


def _correction() -> dict[str, object]:
    return {
        "id": "correction-1",
        "user_id": "user-1",
        "juya_number": "JY000000000001",
        "nickname": "学习者",
        "wechat_id": "wx-private",
        "reason": "微信号需要更正",
        "status": "PENDING",
        "created_at": "2026-09-29T00:00:00Z",
        "processed_at": None,
        "timeline": [
            {
                "status": "PENDING",
                "actor_type": "USER",
                "actor_id": "user-1",
                "event_type": "CONTACT_CORRECTION_CREATED",
                "occurred_at": "2026-09-29T00:00:00Z",
            }
        ],
    }


@pytest.mark.asyncio
async def test_wechat_search_uses_current_signed_post_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["admin_id"] = request.headers["X-Admin-Id"]
        seen["signature"] = request.headers["X-Juya-Signature"]
        return httpx.Response(200, json={"items": [{"public_id": "user-1"}], "has_more": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient(
            "https://miniapp.internal",
            b"secret",
            http=http,
            clock=lambda: NOW,
            nonce_factory=lambda: "nonce-1",
        )
        user_ids = await client.search_user_ids_by_wechat("wx-private", "admin-1")

    body = b'{"wechat_id":"wx-private"}'
    assert user_ids == ("user-1",)
    assert seen["method"] == "POST"
    assert seen["url"] == "https://miniapp.internal/internal/v1/users/search"
    assert seen["body"] == body
    assert seen["admin_id"] == "admin-1"
    assert seen["signature"] == sign_request(
        "POST",
        "/internal/v1/users/search",
        int(NOW.timestamp()),
        "nonce-1",
        body,
        b"secret",
    )


@pytest.mark.asyncio
async def test_get_learning_overview_signs_an_empty_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        seen["signature"] = request.headers["X-Juya-Signature"]
        seen["admin_id"] = request.headers["X-Admin-Id"]
        return httpx.Response(
            200,
            json={
                "open_scene_completed_count": 3,
                "learning_days": 36,
                "favorite_count": 42,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient(
            "https://miniapp.internal",
            b"secret",
            http=http,
            clock=lambda: NOW,
            nonce_factory=lambda: "nonce-2",
        )
        overview = await client.get_learning_overview("user-1", "admin-1")

    path = "/internal/v1/users/user-1/learning-overview"
    assert seen == {
        "method": "GET",
        "body": b"",
        "signature": sign_request("GET", path, int(NOW.timestamp()), "nonce-2", b"", b"secret"),
        "admin_id": "admin-1",
    }
    assert overview.open_scene_completed_count == 3
    assert overview.learning_days == 36
    assert overview.favorite_count == 42


@pytest.mark.asyncio
async def test_contact_projection_parses_full_metadata_and_degrades_only_when_unavailable() -> None:
    def success(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Admin-Id"] == "admin-1"
        return httpx.Response(200, json={"contacts": [_contact()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        result = await client.get_contact_projections(("user-1",), "admin-1")

    assert result.degraded is False
    assert result.contacts[0].change_pending is True
    assert result.contacts[0].updated_at == NOW

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        degraded = await client.get_contact_projections(("user-1",), "admin-1")

    assert degraded.degraded is True
    assert degraded.contacts == ()


@pytest.mark.asyncio
async def test_correction_and_contact_commands_use_admin_and_idempotency_headers() -> None:
    seen: list[tuple[str, str, dict[str, object] | None, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append(
            (
                request.method,
                request.url.path,
                body,
                request.headers["X-Admin-Id"],
                request.headers.get("X-Idempotency-Key"),
            )
        )
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={"items": [_correction()], "total": 1, "page": 1, "page_size": 20},
            )
        if request.url.path.endswith("/contact-status"):
            return httpx.Response(200, json={"contact": _contact()})
        if request.url.path.endswith("/verify-change"):
            return httpx.Response(200, json={"contact": _contact()})
        if request.url.path.endswith("/decision"):
            return httpx.Response(
                200,
                json={
                    "id": "correction-1",
                    "status": "APPROVED",
                    "processed_at": "2026-09-29T00:00:00Z",
                },
            )
        return httpx.Response(200, json=_correction())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        page = await client.list_contact_corrections("PENDING", 1, 20, "admin-1")
        detail = await client.get_contact_correction("correction-1", "admin-1")
        status = await client.update_contact_status("user-1", "CONTACTED", "admin-1")
        verified = await client.verify_contact_change("user-1", "admin-1")
        decision = await client.decide_contact_correction(
            "correction-1", "APPROVED", "admin-1", "idem-1"
        )

    assert page.total == 1
    assert page.items == (detail,)
    assert detail.timeline[0].event_type == "CONTACT_CORRECTION_CREATED"
    assert status.contact_status == "PENDING"
    assert verified.change_pending is True
    assert decision.status == "APPROVED"
    assert seen == [
        (
            "POST",
            "/internal/v1/contact-corrections/search",
            {"status": "PENDING", "page": 1, "page_size": 20},
            "admin-1",
            None,
        ),
        (
            "GET",
            "/internal/v1/contact-corrections/correction-1",
            None,
            "admin-1",
            None,
        ),
        (
            "POST",
            "/internal/v1/users/user-1/contact-status",
            {"status": "CONTACTED"},
            "admin-1",
            None,
        ),
        (
            "POST",
            "/internal/v1/users/user-1/contact/verify-change",
            {},
            "admin-1",
            None,
        ),
        (
            "POST",
            "/internal/v1/contact-corrections/correction-1/decision",
            {"decision": "APPROVED"},
            "admin-1",
            "idem-1",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "upstream_code"),
    [
        (404, "CONTACT_CORRECTION_NOT_FOUND"),
        (409, "IDEMPOTENCY_KEY_REUSED"),
        (422, "CORRECTION_DECISION_INVALID"),
    ],
)
async def test_safe_upstream_client_errors_keep_status_and_code(
    status_code: int, upstream_code: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"code": upstream_code, "message": "upstream detail", "details": {}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        with pytest.raises(AppError) as error:
            await client.get_contact_correction("correction-1", "admin-1")

    assert error.value.status_code == status_code
    assert error.value.code == upstream_code
    assert error.value.message == "用户服务请求未完成"


@pytest.mark.asyncio
async def test_write_timeout_raises_and_malformed_success_is_rejected() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        with pytest.raises(AppError) as unavailable:
            await client.update_contact_status("user-1", "CONTACTED", "admin-1")
    assert unavailable.value.code == "MINIAPP_API_UNAVAILABLE"

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"learning_days": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        with pytest.raises(AppError) as invalid:
            await client.get_learning_overview("user-1", "admin-1")
    assert invalid.value.code == "MINIAPP_API_INVALID_RESPONSE"
