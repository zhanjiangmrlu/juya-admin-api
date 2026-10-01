import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from juya_admin_api.infrastructure.security.service_hmac import sign_request
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.modules.user_projection.deletion_service import (
    DeletionCleanupService,
    InMemoryDeletionRepository,
)
from juya_admin_api.modules.user_projection.router import create_internal_deletion_router


@pytest.mark.asyncio
async def test_current_mini_deletion_contract_is_mounted_and_authenticated() -> None:
    seen = []

    async def auth() -> object:
        seen.append(True)
        return object()

    app = FastAPI()
    app.include_router(
        create_internal_deletion_router(
            DeletionCleanupService(InMemoryDeletionRepository()), current_service=auth
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/v1/account-deletions",
            json={
                "user_id": "U" * 26,
                "deletion_request_id": "R" * 26,
                "event_id": "E" * 26,
            },
        )
    assert response.status_code == 200
    assert seen == [True]
    assert response.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_cleanup_callback_uses_exact_consumer_body_and_fresh_hmac() -> None:
    now = datetime.now(UTC)
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/internal/v1/users/user-1/deletion-cleanup-result"
        assert json.loads(request.content) == {
            "deletion_request_id": "request-1",
            "succeeded": True,
        }
        assert request.headers["X-Juya-Signature"] == sign_request(
            "POST",
            request.url.path,
            int(now.timestamp()),
            request.headers["X-Juya-Nonce"],
            request.content,
            b"test-secret",
        )
        return httpx.Response(200, json={"status": "DELETED"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = MiniappApiClient("http://test", b"test-secret", http=http, clock=lambda: now)
        await client.record_deletion_cleanup_result("user-1", "request-1", "event-1")
        await client.record_deletion_cleanup_result("user-1", "request-1", "event-1")
    assert requests[0].headers["X-Juya-Nonce"] != requests[1].headers["X-Juya-Nonce"]


def test_expired_draft_cleanup_is_registered_on_assets_queue_and_beat() -> None:
    from juya_admin_api.infrastructure.tasks import schedules
    from juya_admin_api.infrastructure.tasks.celery_app import celery_app

    assert schedules.cleanup_expired_drafts.name == "juya.content.assets.cleanup_expired_drafts"
    assert (
        celery_app.conf.beat_schedule["cleanup-expired-drafts"]["task"]
        == schedules.cleanup_expired_drafts.name
    )
