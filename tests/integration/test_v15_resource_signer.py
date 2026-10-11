"""Isolated SQL checks for uploaded pronunciation-to-media fixed reference resolution."""

import hashlib
import os
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.pronunciation.domain import Target
from juya_admin_api.modules.pronunciation.models import targets
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.resources import SQLResourceSigner
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_sql_uploaded_resource_pins_media_and_independent_security_status() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    asset, audio_target, audio_version, pronunciation = [
        new_ulid(datetime.now(UTC)) for _ in range(4)
    ]
    signer = SQLResourceSigner(AsyncMock())
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,asset_type,"
                    "content_type,size_bytes,sha256,status,security_status,"
                    "created_by,created_at,duration_ms) "
                    "VALUES(:id,:key,'audio','audio/mpeg',100,:sha,'CONFIRMED','PASSED','test',:now,1000)"
                ),
                {
                    "id": asset,
                    "key": f"sealed/media/audio/{asset}.mp3",
                    "sha": hashlib.sha256(asset.encode()).hexdigest(),
                    "now": datetime.now(UTC),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,target_type) "
                    "VALUES(:id,:key,'WORD')"
                ),
                {"id": audio_target, "key": "v15-resource:" + audio_target},
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version(public_id,target_id,asset_id,"
                    "version_no,source,status,created_at) "
                    "SELECT :id,t.id,m.id,1,'MANUAL','ACTIVE',:now "
                    "FROM audio_target t,media_asset m "
                    "WHERE t.public_id=:target AND m.public_id=:asset"
                ),
                {
                    "id": audio_version,
                    "target": audio_target,
                    "asset": asset,
                    "now": datetime.now(UTC),
                },
            )
            await session.execute(
                insert(targets).values(
                    **asdict(
                        Target(
                            pronunciation,
                            "medium",
                            uploaded_target_id=audio_target,
                            uploaded_resource_version=audio_version,
                        )
                    ),
                    confirmed_by="test",
                    confirmed_at=1,
                    created_at=1,
                )
            )
        request = ResourceRequest(
            "user",
            "dialogue",
            "scene",
            "revision",
            pronunciation,
            audio_version,
            "WORD",
            favorite_id="favorite",
        )
        async with sessions() as session, session.begin():
            assert (await signer.audio(session, request, [pronunciation]))["public_id"] == asset
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE media_asset SET security_status='BLOCKED' WHERE public_id=:id"),
                {"id": asset},
            )
        async with sessions() as session, session.begin():
            with pytest.raises(AppError):
                await signer.audio(session, request, [pronunciation])
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM pronunciation_target WHERE public_id=:id"), {"id": pronunciation}
            )
            await session.execute(
                text("DELETE FROM audio_version WHERE public_id=:id"), {"id": audio_version}
            )
            await session.execute(
                text("DELETE FROM audio_target WHERE public_id=:id"), {"id": audio_target}
            )
            await session.execute(
                text("DELETE FROM media_asset WHERE public_id=:id"), {"id": asset}
            )
        await engine.dispose()
