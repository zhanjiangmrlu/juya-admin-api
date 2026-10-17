from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_store import Session, Sessions

from juya_admin_api.modules.short_reading.internal_router import (
    create_short_reading_internal_router,
)
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import install_error_handlers


@pytest.mark.asyncio
async def test_preview_authorization_never_opens_full_text_or_audio():
    store = ShortReadingStore(Sessions(Session()))
    store.published_detail_within = AsyncMock(
        side_effect=AssertionError("must not return full text")
    )
    calls = []

    async def context():
        return {"purpose": "PREVIEW", "user_id": "user"}

    async def authorize(session, identity, ctx):
        calls.append(ctx["purpose"])
        return ctx["purpose"] == "PREVIEW"

    signer = AsyncMock()
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_short_reading_internal_router(
            store, current_context=context, authorizer=authorize, resource_signer=signer
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (await c.post("/internal/v1/short-readings/r/open")).status_code == 403
        assert (
            await c.get("/internal/v1/short-readings/r/resources/a/signed-url")
        ).status_code == 403
    assert calls == ["CONTENT", "CONTENT"]
    signer.assert_not_awaited()
    store.published_detail_within.assert_not_awaited()
