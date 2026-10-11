"""Actual PCM bytes, SQL resources and FFmpeg; cloud safety is an explicit fixture."""

import hashlib
import io
import math
import os
import shutil
import struct
import wave
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.v15_runtime import build_v15_services
from juya_admin_api.integrations.content_security.protocol import SecurityResult
from juya_admin_api.integrations.oss.provider import ObjectMetadata
from juya_admin_api.modules.audio_pipeline.factory import create_audio_pipeline
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_authoring_router import create_authoring_router
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.media.repository import SQLAlchemyMediaRepository
from juya_admin_api.modules.media.service import MediaAsset, MediaService
from juya_admin_api.shared.errors import AppError, install_error_handlers
from juya_admin_api.shared.ids import new_ulid


def generated_wav(frequency: int) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        # Two separate tones with a real pause; boundaries below are explicit manual fixture data.
        samples = [
            round(8000 * math.sin(2 * math.pi * frequency * index / 16000))
            if index < 16000 or index >= 19200
            else 0
            for index in range(48000)
        ]
        output.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return stream.getvalue()


class PrivateByteOSS:
    def __init__(self):
        self.objects = {}
        self.signatures = []

    async def freeze_bytes(self, data, asset_type, content_type):
        digest = hashlib.sha256(data).hexdigest()
        key = f"sealed/media/{asset_type}/{digest}/{uuid4().hex}.wav"
        self.objects[key] = (bytes(data), content_type)
        return key

    async def read_bytes(self, key, max_bytes):
        data = self.objects[key][0]
        assert len(data) <= max_bytes
        return data

    async def head_object(self, key):
        data, mime = self.objects[key]
        return ObjectMetadata(key, len(data), mime, hashlib.sha256(data).hexdigest(), {})

    async def sign_get_url(self, key, expires_in):
        assert key in self.objects and expires_in <= 60
        self.signatures.append((key, expires_in))
        return f"https://private-fixture.invalid/{key}?ttl={expires_in}"


class FixtureSafety:
    # Deliberately controllable exception fixture; never represents actual cloud safety acceptance.
    def __init__(self):
        self.status = "PASSED"
        self.calls = []

    async def scan_audio(self, object_key):
        self.calls.append(object_key)
        return SecurityResult("fixture-audio-review", self.status)

    async def poll_audio(self, request_id):
        return SecurityResult(request_id, self.status)


@pytest.mark.asyncio
async def test_real_audio_authoring_preview_publish_and_immutable_resplit(tmp_path):
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "real FFmpeg/ffprobe required by CI; never mock successful slicing"
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    oss, safety = PrivateByteOSS(), FixtureSafety()
    media_repository = SQLAlchemyMediaRepository(sessions)
    media = MediaService(
        oss, media_repository, signed_url_ttl_seconds=60, security=safety, ffprobe_path=ffprobe
    )
    settings = Settings(
        _env_file=None,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        import_storage_path=str(tmp_path),
        content_security_enabled=True,
        internal_hmac_secret="isolated-audio-credential-secret-32bytes",
    )
    runtime = build_v15_services(settings, sessions, oss, media, safety)
    authoring, store, proof = runtime.authoring, runtime.production, runtime.proofreading
    service = ContentService(
        SQLAlchemyContentRepository(sessions, check_confirmation=proof.is_confirmed_within)
    )
    actor = new_ulid(datetime.now(UTC))

    async def source_audio(frequency, role="WHOLE"):
        data = generated_wav(frequency)
        facts = await inspect_media(data, "audio", ffprobe)
        key = await oss.freeze_bytes(data, "audio", facts.content_type)
        asset, target, version = [new_ulid(datetime.now(UTC)) for _ in range(3)]
        await media_repository.save(
            MediaAsset(
                asset,
                key,
                "audio",
                facts.content_type,
                len(data),
                facts.sha256,
                "CONFIRMED",
                "PASSED",
                actor,
                datetime.now(UTC),
                duration_ms=facts.duration_ms,
            )
        )
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,target_type) "
                    "VALUES(:id,:key,:role)"
                ),
                {"id": target, "key": "audio-flow:" + uuid4().hex, "role": role},
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,source,"
                    "status,created_by,created_at) "
                    "SELECT :version,t.id,m.id,1,'MANUAL','ACTIVE',:actor,"
                    "UTC_TIMESTAMP(6) FROM audio_target t,media_asset m WHERE t.public_id=:target "
                    "AND m.public_id=:asset"
                ),
                {"version": version, "target": target, "asset": asset, "actor": actor},
            )
        return dict(
            target_id=target, version_id=version, asset_id=asset, duration_ms=facts.duration_ms
        )

    async def admin():
        return SimpleNamespace(admin_user_id=1)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(create_authoring_router(authoring, current_admin_write=admin))
    try:
        source = await source_audio(441 + int(uuid4().hex[:3], 16))
        series = await store.create_series("Real audio authoring", uuid4().hex, None)
        identity = await store.create_scene(series["id"], "dialogue", actor=actor, key=uuid4().hex)
        scene = await service.get_scene(identity)
        draft = await service.save_revision(
            scene.draft_revision_id,
            {
                "schema_version": 2,
                "template_type": "dialogue",
                "title_en": "Audio fixture",
                "title_zh": "音频夹具",
                "copyright": "Owned generated tones",
                "source": "Test owner",
                "audio": source,
                "dialogue": [
                    {
                        "id": "manual-first",
                        "speaker": "A",
                        "english": "Hello there.",
                        "chinese": "你好。",
                    },
                    {
                        "id": "manual-second",
                        "speaker": "B",
                        "english": "Good morning.",
                        "chinese": "早上好。",
                    },
                ],
            },
            expected_version=1,
            actor_id=actor,
        )
        assert not draft.content["vocabulary"]
        pipeline = create_audio_pipeline(
            sessions=sessions,
            oss=oss,
            current_input=authoring.current_input,
            locked_input=authoring.locked_input,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
        )
        with pytest.raises(AppError, match="边界"):
            await authoring.current_input(draft.id, "manual-first")
        with pytest.raises(AppError) as unpublished:
            await store.publish(draft, actor, uuid4().hex, datetime.now(UTC))
        assert unpublished.value.code == "PUBLISH_CHECK_FAILED"
        originals = []
        for sentence, start, end in [("manual-first", 150, 950), ("manual-second", 1300, 2750)]:
            with pytest.raises(AppError) as unchecked:
                await authoring.confirm_boundary(
                    draft.id, sentence, draft.version, start, end, actor, False
                )
            assert unchecked.value.code == "SENTENCE_BOUNDARY_INVALID"
            receipt = await authoring.confirm_boundary(
                draft.id, sentence, draft.version, start, end, actor, True
            )
            draft = await service.get_revision(draft.id)
            replacement_claim = None
            if sentence == "manual-first":
                stages = runtime.imports.stages
                failed_claim = await stages.claim(
                    receipt["stage_id"], "failed-local-worker", datetime.now(UTC)
                )
                assert failed_claim
                await stages.fail(failed_claim, "LOCAL_DECODE_FAILED", datetime.now(UTC), False)
                retried = await authoring.confirm_boundary(
                    draft.id, sentence, draft.version, start, end, actor, True
                )
                assert retried["stage_id"] == receipt["stage_id"]
                assert retried["dependency_hash"] == receipt["dependency_hash"]
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT status FROM content_import_stage WHERE public_id=:id"),
                            {"id": receipt["stage_id"]},
                        )
                        == "PENDING"
                    )
                with pytest.raises(AppError) as old_finish:
                    await stages.finish(failed_claim, {"obsolete": True}, datetime.now(UTC))
                assert old_finish.value.code == "IMPORT_STAGE_LEASE_LOST"
                replacement_claim = await stages.claim(
                    receipt["stage_id"], "replacement-local-worker", datetime.now(UTC)
                )
                assert replacement_claim
                draft = await service.get_revision(draft.id)
            current = await authoring.current_input(draft.id, sentence)
            assert (current.boundary.start_ms, current.boundary.end_ms) == (start, end)
            assert current.dependency(authoring.encoding_config) == receipt["dependency_hash"]
            result = await pipeline.split(draft.id, sentence)
            assert result.actual_duration_ms == pytest.approx(end - start, abs=30)
            assert await pipeline.split(draft.id, sentence) == result
            if replacement_claim is not None:
                await stages.finish(replacement_claim, asdict(result), datetime.now(UTC))
                reattached = await authoring.confirm_boundary(
                    draft.id, sentence, draft.version, start, end, actor, True
                )
                assert reattached["stage_id"] == receipt["stage_id"]
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT status FROM content_import_stage WHERE public_id=:id"),
                            {"id": receipt["stage_id"]},
                        )
                        == "PENDING"
                    )
                cached_claim = await stages.claim(
                    receipt["stage_id"], "cached-local-worker", datetime.now(UTC)
                )
                assert cached_claim
                assert await pipeline.split(draft.id, sentence) == result
                await stages.finish(cached_claim, asdict(result), datetime.now(UTC))
                with pytest.raises(AppError) as old_success:
                    await stages.finish(replacement_claim, {"obsolete": True}, datetime.now(UTC))
                assert old_success.value.code == "IMPORT_STAGE_LEASE_LOST"
                draft = await service.get_revision(draft.id)
            slice_asset = await media_repository.get(result.asset_id)
            immutable = await oss.read_bytes(slice_asset.object_key, 50 * 1024 * 1024)
            assert immutable != await oss.read_bytes(
                (await media_repository.get(source["asset_id"])).object_key, 50 * 1024 * 1024
            )
            assert hashlib.sha256(immutable).hexdigest() == slice_asset.sha256
            assert (await oss.head_object(slice_asset.object_key)).sha256 == slice_asset.sha256
            originals.append((result, slice_asset.object_key, immutable))
            if sentence == "manual-first":
                safety.status = "BLOCKED"
                with pytest.raises(AppError) as rejected_audio:
                    await authoring.slice_preview(draft.id, sentence, result.dependency_hash)
                assert rejected_audio.value.code == "SLICE_REVIEW_PENDING"
                safety.status = "PASSED"
                stored_mime = oss.objects[slice_asset.object_key][1]
                oss.objects[slice_asset.object_key] = (b"corrupted audio", stored_mime)
                with pytest.raises(AppError) as corrupted:
                    await authoring.slice_preview(draft.id, sentence, result.dependency_hash)
                assert corrupted.value.code == "MEDIA_DECODE_FAILED"
                oss.objects[slice_asset.object_key] = (immutable, stored_mime)
            with pytest.raises(AppError) as unheard:
                await authoring.confirm_slice(
                    draft.id, sentence, draft.version, result.dependency_hash, actor, False
                )
            assert unheard.value.code == "SLICE_LISTENING_REQUIRED"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture"
            ) as client:
                preview = await client.get(
                    f"/api/v1/admin/content/revisions/{draft.id}/sentences/{sentence}/slice-preview",
                    params={"dependency_hash": result.dependency_hash},
                )
            assert preview.status_code == 200, preview.text
            assert preview.headers["cache-control"] == "no-store"
            assert preview.json()["expires_in"] == oss.signatures[-1][1] == 60
            assert preview.json()["duration_ms"] == result.actual_duration_ms
            assert safety.calls
            await authoring.confirm_slice(
                draft.id, sentence, draft.version, result.dependency_hash, actor, True
            )
            draft = await service.get_revision(draft.id)
        digest = snapshot_hash(draft.content)
        cohort = await proof.create(
            [
                dict(
                    revision_id=draft.id,
                    candidate_hash=digest,
                    content_type="dialogue",
                    content_id=draft.scene_id,
                )
            ],
            actor,
        )
        await proof.confirm(
            cohort["public_id"],
            draft.id,
            digest,
            actor,
            {"text_checked": True, "audio_listened": True},
        )
        await proof.complete(cohort["public_id"], actor)
        published = await service.publish_revision(
            draft.id, actor, uuid4().hex, datetime.now(UTC), expected_version=draft.version
        )
        assert published.revision_id == draft.id
        full = await store.full_scene(draft.scene_id)
        assert full["revision_id"] == draft.id
        for result, key, _data in originals:
            by_version = await store.resource(
                draft.scene_id, draft.id, result.resource_version, published=True
            )
            by_target = await store.resource(
                draft.scene_id, draft.id, result.target_id, published=True
            )
            assert by_version["object_key"] == by_target["object_key"] == key
        with pytest.raises(AppError) as unrelated_resource:
            await store.resource(draft.scene_id, draft.id, "unreferenced", published=True)
        assert unrelated_resource.value.code == "RESOURCE_NOT_REFERENCED"
        with pytest.raises(AppError) as immutable_revision:
            await authoring.confirm_boundary(
                draft.id, "manual-first", draft.version, 200, 900, actor, True
            )
        assert immutable_revision.value.code == "AUTHORING_REVISION_INVALID"

        # Explicit copy is editable; changing one sentence does not overwrite the published version.
        copy = await service.create_revision(draft.scene_id, draft.id, actor, datetime.now(UTC))
        changed = dict(copy.content)
        changed["dialogue"] = [dict(row) for row in copy.content["dialogue"]]
        changed["dialogue"][0]["english"] = "Hello again."
        copy = await service.save_revision(
            copy.id, changed, expected_version=copy.version, actor_id=actor
        )
        assert copy.content["dialogue"][0]["slice_version"] is None
        assert copy.content["dialogue"][1]["slice_version"] == originals[1][0].resource_version
        await authoring.confirm_boundary(
            copy.id, "manual-first", copy.version, 220, 900, actor, True
        )
        copy = await service.get_revision(copy.id)
        remade = await pipeline.split(copy.id, "manual-first")
        assert remade.resource_version != originals[0][0].resource_version
        assert remade.dependency_hash != originals[0][0].dependency_hash
        with pytest.raises(AppError) as outdated:
            await authoring.slice_preview(copy.id, "manual-first", originals[0][0].dependency_hash)
        assert outdated.value.code == "SLICE_DEPENDENCY_CHANGED"
        replaced = dict(copy.content, audio=await source_audio(999))
        copy = await service.save_revision(
            copy.id, replaced, expected_version=copy.version, actor_id=actor
        )
        assert all(
            not row["timing_confirmed"] and row["slice_version"] is None
            for row in copy.content["dialogue"]
        )
        await authoring.confirm_boundary(
            copy.id, "manual-first", copy.version, 220, 900, actor, True
        )
        copy = await service.get_revision(copy.id)
        new_source_slice = await pipeline.split(copy.id, "manual-first")
        assert new_source_slice.resource_version != remade.resource_version
        assert new_source_slice.dependency_hash != remade.dependency_hash
        # A real registered WORD target must never serve as the scene's whole recording.
        wrong_role = dict(copy.content, audio=await source_audio(1177, "WORD"))
        copy = await service.save_revision(
            copy.id, wrong_role, expected_version=copy.version, actor_id=actor
        )
        with pytest.raises(AppError) as role_conflict:
            await authoring.confirm_boundary(
                copy.id, "manual-first", copy.version, 100, 800, actor, True
            )
        assert role_conflict.value.code == "SOURCE_AUDIO_NOT_READY"
        for result, key, data in originals:
            assert await oss.read_bytes(key, 50 * 1024 * 1024) == data
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT status FROM sentence_audio_slice WHERE public_id=:id"),
                        {"id": result.id},
                    )
                    == "CONFIRMED"
                )
        assert (await service.get_revision(draft.id)).content == draft.content
    finally:
        await runtime.client.aclose()
        await engine.dispose()
