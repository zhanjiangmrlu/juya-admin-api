"""Isolated MySQL projection authority, replay, cancellation and stale event acceptance."""

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.v15_contracts import FavoriteResourceEvent
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_projection_replay_and_old_event_cannot_restore_cancelled_reference() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    svc = V15AccessService(
        sessions,
        content_loader=AsyncMock(),
        authorizer=AsyncMock(),
        resource_signer=AsyncMock(),
        credential_secret=b"unit-private-signing-secret",
        favorite_authority=AsyncMock(),
    )
    identity = uuid4().hex
    event = FavoriteResourceEvent(
        event_id=uuid4().hex,
        sequence=1,
        favorite_id=identity,
        snapshot_revision_id=uuid4().hex,
        active=True,
        content_type="dialogue",
        content_id=uuid4().hex,
        revision_id=uuid4().hex,
        image_asset_id="saved-image",
    )
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO favorite_resource_reference(favorite_id,"
                    "snapshot_revision_id,content_type,content_id,revision_id,image_asset_id,"
                    "pronunciation_target_id,resource_version,active,event_id,sequence,updated_at) "
                    "VALUES(:favorite_id,:snapshot_revision_id,:content_type,:content_id,:revision_id,"
                    ":image_asset_id,:pronunciation_target_id,:resource_version,:active,:event_id,"
                    ":sequence,:now)"
                ),
                {**event.model_dump(), "now": datetime.now(UTC)},
            )
        assert (await svc.project_event(event))["applied"] is True
        assert (await svc.project_event(event))["applied"] is False
        with pytest.raises(AppError):
            await svc.project_event(event.model_copy(update={"image_asset_id": "forged-image"}))
        cancelled = event.model_copy(
            update={"sequence": 2, "event_id": uuid4().hex, "active": False}
        )
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE favorite_resource_reference SET sequence=2,"
                    "event_id=:event,active=0 WHERE favorite_id=:id"
                ),
                {"event": cancelled.event_id, "id": identity},
            )
        assert (await svc.project_event(cancelled))["applied"] is True
        assert (await svc.project_event(event))["reason"] == "STALE"
        async with sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT sequence,active FROM "
                        "favorite_resource_projection WHERE favorite_id=:id"
                    ),
                    {"id": identity},
                )
            ).one()
            assert row.sequence == 2 and not row.active
    finally:
        async with sessions() as session, session.begin():
            for table in ("favorite_resource_projection", "favorite_resource_reference"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE favorite_id=:id"), {"id": identity}
                )
        await engine.dispose()
