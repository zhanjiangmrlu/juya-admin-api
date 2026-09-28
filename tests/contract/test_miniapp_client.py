import json
from datetime import UTC, datetime

import httpx
import pytest

from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_wechat_search_uses_signed_post_body_and_never_query_string() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["signature"] = request.headers["X-Juya-Signature"]
        return httpx.Response(200, json={"user_ids": ["user-1"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient(
            "https://miniapp.internal",
            b"secret",
            http=http,
            clock=lambda: NOW,
            nonce_factory=lambda: "nonce-1",
        )
        user_ids = await client.search_user_ids_by_wechat("wx-private")

    assert user_ids == ("user-1",)
    assert seen["method"] == "POST"
    assert seen["url"] == "https://miniapp.internal/internal/v1/users/search-by-wechat"
    assert seen["body"] == {"wechat_id": "wx-private"}
    assert isinstance(seen["signature"], str)


@pytest.mark.asyncio
async def test_timeout_returns_controlled_degraded_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = MiniappApiClient("https://miniapp.internal", b"secret", http=http)
        result = await client.get_contact_projections(("user-1",))

    assert result.degraded is True
    assert result.contacts == ()
