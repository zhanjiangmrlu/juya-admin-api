"""Real SQL FULL target/CAS and private upload byte validation without cloud calls."""

import asyncio
import hashlib
import io
import os
import shutil
import wave
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.integrations.content_security.protocol import SecurityResult
from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.media.repository import SQLAlchemyMediaRepository
from juya_admin_api.modules.media.service import MediaService
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.modules.short_reading.upload_targets import (
    ShortReadingUploads,
    create_short_reading_upload_router,
)
from juya_admin_api.shared.errors import AppError, install_error_handlers


class ByteUploadOSS:
    def __init__(self):
        self.objects = {}
        self.reads = 0

    async def create_upload_policy(self, prefix, max_bytes, expires_in):
        return UploadPolicy(
            "https://upload-fixture.invalid",
            prefix,
            max_bytes,
            expires_in,
            {"policy": "temporary-fixture-policy"},
        )

    async def head_object(self, key):
        data, mime = self.objects[key]
        return ObjectMetadata(key, len(data), mime, hashlib.sha256(data).hexdigest(), {})

    async def read_bytes(self, key, max_bytes):
        self.reads += 1
        data = self.objects[key][0]
        assert len(data) <= max_bytes
        return data

    async def freeze_bytes(self, data, asset_type, content_type):
        key = f"sealed/media/{asset_type}/{uuid4().hex}.wav"
        self.objects[key] = (bytes(data), content_type)
        return key


class FixtureAudioSafety:
    async def scan_audio(self, _key):
        return SecurityResult("fixture-only", "PASSED")


@pytest.mark.asyncio
async def test_direct_full_upload_target_real_policy_bytes_binding_cas_and_replay():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    ffprobe = shutil.which("ffprobe")
    assert ffprobe, "actual decoder required"
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ShortReadingStore(sessions)
    oss = ByteUploadOSS()
    media = MediaService(
        oss,
        SQLAlchemyMediaRepository(sessions),
        security=FixtureAudioSafety(),
        ffprobe_path=ffprobe,
    )
    uploads = ShortReadingUploads(store, media)
    actor = "1"
    production = ProductionStore(sessions)
    series = await production.create_series("FULL upload", uuid4().hex, None)
    created = await store.command(
        "create", "new", actor, uuid4().hex, {"series_id": series["id"], "content": {}}
    )
    identity, revision = created["content_id"], created["revision_id"]
    payload = {"expected_version": 1, "expected_edit_version": 1}

    async def admin():
        return SimpleNamespace(admin_user_id=1)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(create_short_reading_upload_router(store, media, current_admin_write=admin))
    path = (
        f"/api/v1/admin/content/short-readings/{identity}/revisions/{revision}/audio-upload-target"
    )
    try:
        key = uuid4().hex
        concurrent = await asyncio.gather(
            *[uploads.target(identity, revision, payload, actor, key) for _ in range(2)]
        )
        assert concurrent[0] == concurrent[1]
        target = concurrent[0]
        assert target["target_type"] == "FULL" and target["active_version_id"] is None
        assert await uploads.target(identity, revision, payload, actor, uuid4().hex) == target
        with pytest.raises(AppError) as conflict:
            await uploads.target(
                identity, revision, dict(payload, expected_edit_version=2), actor, key
            )
        assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            response = await client.post(
                path, json=payload, headers={"Idempotency-Key": uuid4().hex}
            )
        assert response.status_code == 200 and response.json()["id"] == target["id"]
        assert response.headers["cache-control"] == "no-store"
        # Upload using the same actual MediaService mechanism exposed by /media endpoints.
        policy = await media.create_upload_policy("audio", actor)
        stream = io.BytesIO()
        with wave.open(stream, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(uuid4().bytes * 4000)
        object_key = policy.object_key_prefix + "full.wav"
        oss.objects[object_key] = (stream.getvalue(), "audio/wav")
        asset = await media.confirm_upload("audio", actor, object_key, datetime.now(UTC))
        assert asset.duration_ms == 2000 and asset.status == "CONFIRMED"
        assert asset.object_key.startswith("sealed/media/audio/")
        assert (await oss.head_object(asset.object_key)).sha256 == asset.sha256
        bind_payload = dict(payload, target_id=target["id"], asset_id=asset.id)
        bind_key = uuid4().hex
        bound = await uploads.bind(identity, revision, bind_payload, actor, bind_key)
        assert bound["audio"]["target_id"] == target["id"]
        assert bound["audio"]["duration_ms"] == 2000
        assert not bound["audio"]["listening_confirmed"]
        assert bound["audio"]["confirmed_asset_id"] is None
        assert bound["audio"]["confirmed_version_id"] is None
        before_replay = oss.reads
        assert await uploads.bind(identity, revision, bind_payload, actor, bind_key) == bound
        assert oss.reads == before_replay
        async with sessions() as session:
            content = await store.revision(session, identity, revision)
            assert content["content_snapshot"]["audio"] == bound["audio"]
            assert content["edit_version"] == 2
            actual = (
                (
                    await session.execute(
                        text(
                            "SELECT v.source,v.status,t.target_type,m.sha256 FROM audio_version v "
                            "JOIN audio_target t ON t.id=v.target_id "
                            "JOIN media_asset m ON m.id=v.asset_id "
                            "WHERE v.public_id=:id"
                        ),
                        {"id": bound["audio"]["version_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert (actual["source"], actual["status"], actual["target_type"]) == (
                "MANUAL",
                "READY",
                "FULL",
            )
            assert actual["sha256"] == asset.sha256
        with pytest.raises(AppError) as stale:
            await uploads.bind(identity, revision, bind_payload, actor, uuid4().hex)
        assert stale.value.code == "CONTENT_VERSION_CONFLICT"
        # An unrelated content/draft cannot borrow this target even with a valid uploaded asset.
        other = await store.command(
            "create", "new", actor, uuid4().hex, {"series_id": series["id"], "content": {}}
        )
        with pytest.raises(AppError) as foreign:
            await uploads.bind(
                other["content_id"], other["revision_id"], bind_payload, actor, uuid4().hex
            )
        assert foreign.value.code == "AUDIO_TARGET_MISMATCH"
        # Current drafts reuse the actual resource without manufacturing listening confirmation.
        async with sessions() as session:
            reading = await store.row(session, identity)
        current_payload = dict(
            bind_payload, expected_version=reading["version"], expected_edit_version=2
        )
        rebound = await uploads.bind(identity, revision, current_payload, actor, uuid4().hex)
        assert rebound["audio"]["version_id"] == bound["audio"]["version_id"]
        assert not rebound["audio"]["listening_confirmed"]
    finally:
        await engine.dispose()
