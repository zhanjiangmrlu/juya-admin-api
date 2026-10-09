import json
from datetime import UTC, datetime

import httpx
import pytest

from juya_admin_api.infrastructure.security.service_hmac import sign_request
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_feedback_message_uses_signed_user_route_and_body_event_id() -> None:
    # 功能:验证真实发件箱载荷使用用户消息接口并在请求体保留幂等事件标识
    # 参数:无
    # 返回:无,断言失败时由 pytest 报告失败
    payload: dict[str, object] = {
        "user_id": "user-1",
        "event_id": "event-1",
        "message_type": "FEEDBACK_RESOLVED",
        "title": "反馈已有结果",
        "summary": "请查看处理结果",
        "related_type": "FEEDBACK",
        "related_id": "feedback-1",
    }
    original = dict(payload)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # 功能:按小程序当前严格接口检查通知地址和载荷并记录签名请求
        # 参数:request 为管理端实际发送的内部通知请求
        # 返回:符合接口时返回消息标识,否则返回真实故障对应的状态码
        seen.append(request)
        if request.url.path != "/internal/v1/users/user-1/messages":
            return httpx.Response(404, json={"detail": "Not Found"})
        body = json.loads(request.content)
        if "user_id" in body or body.get("event_id") != "event-1":
            return httpx.Response(422, json={"detail": "Invalid message body"})
        return httpx.Response(201, json={"id": "message-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient(
            "https://miniapp.internal",
            b"secret",
            http=http,
            clock=lambda: NOW,
            nonce_factory=lambda: "nonce-1",
        )
        await client.create_message(payload, "event-1")
        await client.create_message(payload, "event-1")

    assert payload == original
    assert len(seen) == 2
    for request in seen:
        assert request.method == "POST"
        assert json.loads(request.content) == {
            key: value for key, value in original.items() if key != "user_id"
        }
        assert request.headers["X-Idempotency-Key"] == "event-1"
        assert request.headers["X-Juya-Signature"] == sign_request(
            "POST",
            "/internal/v1/users/user-1/messages",
            int(NOW.timestamp()),
            "nonce-1",
            request.content,
            b"secret",
        )


def _contact(user_id: str = "user-1") -> dict[str, object]:
    # 功能:构造契约测试使用的联系方式投影响应。
    # 参数:
    #     user_id: 目标用户标识;认证仓库中使用管理员数据库主键。
    # 返回:dict[str, object],由本用例预设的数据或所组装的测试资源构成。
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
    # 功能:构造契约测试使用的联系方式修正申请响应。
    # 参数:无。
    # 返回:dict[str, object],由本用例预设的数据或所组装的测试资源构成。
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
    # 功能:验证微信号搜索使用当前签名 POST 契约。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # 功能:检查或记录上游请求并返回当前用例预设的 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["admin_id"] = request.headers["X-Admin-Id"]
        seen["signature"] = request.headers["X-Juya-Signature"]
        return httpx.Response(200, json={"items": [{"public_id": "user-1"}], "has_more": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
        # 参数: 无。
        # 返回: 对应测试时间或 UTC 当前时间。
        # 匿名函数: 注入确定的签名随机值, 供跨服务签名结果比较。
        # 参数: 无。
        # 返回: 本用例固定的 nonce 字符串。
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
    # 功能:验证学习概览请求对空请求体正确签名。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # 功能:检查或记录上游请求并返回当前用例预设的 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
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
        # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
        # 参数: 无。
        # 返回: 对应测试时间或 UTC 当前时间。
        # 匿名函数: 注入确定的签名随机值, 供跨服务签名结果比较。
        # 参数: 无。
        # 返回: 本用例固定的 nonce 字符串。
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

    # 功能:验证联系方式投影保留完整元数据且仅在服务不可用时降级。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    def success(request: httpx.Request) -> httpx.Response:
        # 功能:检查管理员调用上下文并返回正常联系方式投影。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
        assert request.headers["X-Admin-Id"] == "admin-1"
        return httpx.Response(200, json={"contacts": [_contact()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        result = await client.get_contact_projections(("user-1",), "admin-1")

    assert result.degraded is False
    assert result.contacts[0].change_pending is True
    assert result.contacts[0].updated_at == NOW

    def timeout(request: httpx.Request) -> httpx.Response:
        # 功能:模拟上游 HTTP 连接超时,供错误处理断言。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        degraded = await client.get_contact_projections(("user-1",), "admin-1")

    assert degraded.degraded is True
    assert degraded.contacts == ()


@pytest.mark.asyncio
async def test_correction_and_contact_commands_use_admin_and_idempotency_headers() -> None:
    # 功能:验证联系方式及修正命令携带管理员和幂等请求头。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    seen: list[tuple[str, str, dict[str, object] | None, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # 功能:检查或记录上游请求并返回当前用例预设的 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
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

    # 功能:验证上游安全错误保留状态码和错误代码。
    # 参数:
    #     status_code: 模拟上游 HTTP 响应状态码。
    #     upstream_code: 模拟上游业务错误代码。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    def handler(_request: httpx.Request) -> httpx.Response:
        # 功能:检查或记录上游请求并返回当前用例预设的 HTTP 响应。
        # 参数:
        #     _request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。 当前替身保
        #       留该形参以兼容调用接口。
        # 返回:预设 HTTP 响应。
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

    # 功能:验证写请求超时及格式异常的成功响应被拒绝。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    def timeout(request: httpx.Request) -> httpx.Response:
        # 功能:模拟上游 HTTP 连接超时,供错误处理断言。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:不产生正常结果;抛出当前用例预设的错误。
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        with pytest.raises(AppError) as unavailable:
            await client.update_contact_status("user-1", "CONTACTED", "admin-1")
    assert unavailable.value.code == "MINIAPP_API_UNAVAILABLE"

    def malformed(_request: httpx.Request) -> httpx.Response:
        # 功能:返回不符合约定格式的响应,供契约校验断言。
        # 参数:
        #     _request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。 当前替身保
        #       留该形参以兼容调用接口。
        # 返回:预设 HTTP 响应。
        return httpx.Response(200, json={"learning_days": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(malformed)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        with pytest.raises(AppError) as invalid:
            await client.get_learning_overview("user-1", "admin-1")
    assert invalid.value.code == "MINIAPP_API_INVALID_RESPONSE"
