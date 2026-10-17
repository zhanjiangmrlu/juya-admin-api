"""Real SQL, worker orchestration and FFmpeg bytes with an explicit ASR/safety fixture."""

import os
import shutil
from datetime import UTC, datetime
from secrets import token_hex
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import v15 as worker
from juya_admin_api.infrastructure.v15_runtime import build_v15_services
from juya_admin_api.integrations.asr.aliyun import ASRCandidate, ASRSegment, ASRWord
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_auto_audio import AutoAudioService
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_import.admin_stages import StageAdminStore
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.media.repository import SQLAlchemyMediaRepository
from juya_admin_api.modules.media.service import MediaAsset, MediaService
from juya_admin_api.shared.ids import new_ulid
from tests.integration.test_v15_audio_authoring_flow import (
    FixtureSafety,
    PrivateByteOSS,
    generated_wav,
)


class FixtureASR:
    model = "fixture-asr-words"

    def __init__(self):
        self.submissions = 0
        self.task_id = "fixture-" + uuid4().hex

    async def submit(self, url):
        assert url.startswith("https://private-fixture.invalid/sealed/media/audio/")
        self.submissions += 1
        return self.task_id

    async def poll(self, task_id):
        assert task_id == self.task_id
        return {
            "output": {
                "task_status": "SUCCEEDED",
                "results": [{"transcription_url": "fixture-result"}],
            },
            "usage": {"seconds": 3},
            "request_id": self.task_id,
        }

    async def parse_result(self, url):
        assert url == "fixture-result"
        return ASRCandidate(
            "Hello there. Good morning.",
            (),
            segments=(
                ASRSegment(
                    "Hello there. Good morning.",
                    150,
                    2750,
                    (
                        ASRWord("Hello", 150, 500),
                        ASRWord("there", 500, 950),
                        ASRWord("Good", 1300, 1700),
                        ASRWord("morning", 1700, 2750),
                    ),
                    0,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_saved_audio_runs_asr_slices_preview_and_sampled_publication(tmp_path, monkeypatch):
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg and ffprobe
    engine = create_async_engine(url.replace("mysql+pymysql", "mysql+asyncmy"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    oss, safety, asr = PrivateByteOSS(), FixtureSafety(), FixtureASR()
    repository = SQLAlchemyMediaRepository(sessions)
    media = MediaService(
        oss, repository, signed_url_ttl_seconds=60, security=safety, ffprobe_path=ffprobe
    )
    settings = Settings(
        _env_file=None,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        import_storage_path=str(tmp_path),
        content_security_enabled=True,
        # 隔离测试显式提供随机 HMAC 密钥,不依赖服务器环境中的密钥
        internal_hmac_secret=SecretStr(token_hex(32)),
    )
    runtime = build_v15_services(settings, sessions, oss, media, safety)
    runtime.candidates.asr = asr
    runtime.candidates.asr_daily_seconds_limit = 100
    monkeypatch.setattr(
        worker,
        "build_runtime",
        lambda _: SimpleNamespace(v15=runtime, engine=engine, close=AsyncMock()),
    )
    service = ContentService(
        SQLAlchemyContentRepository(
            sessions, check_confirmation=runtime.proofreading.is_confirmed_within
        )
    )
    actor = new_ulid(datetime.now(UTC))
    try:
        data = generated_wav(441 + int(uuid4().hex[:4], 16))
        facts = await inspect_media(data, "audio", ffprobe)
        key = await oss.freeze_bytes(data, "audio", facts.content_type)
        asset, target, version = [new_ulid(datetime.now(UTC)) for _ in range(3)]
        await repository.save(
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
                    "VALUES(:id,:key,'WHOLE')"
                ),
                {"id": target, "key": "auto-fixture:" + uuid4().hex},
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,source,"
                    "status,created_by,created_at) SELECT :version,t.id,m.id,1,'MANUAL','READY',"
                    ":actor,UTC_TIMESTAMP(6) FROM audio_target t,media_asset m "
                    "WHERE t.public_id=:target AND m.public_id=:asset"
                ),
                {"version": version, "actor": actor, "target": target, "asset": asset},
            )
        series = await runtime.production.create_series(
            "Automatic worker fixture", uuid4().hex, None
        )
        identity = await runtime.production.create_scene(
            series["id"], "dialogue", actor=actor, key=uuid4().hex
        )
        scene = await service.get_scene(identity)
        draft = await service.save_revision(
            scene.draft_revision_id,
            {
                "schema_version": 2,
                "template_type": "dialogue",
                "title_en": "Hello",
                "title_zh": "你好",
                "copyright": "Owned fixture tones",
                "source": "Local fixture",
                "audio": {
                    "target_id": target,
                    "version_id": version,
                    "asset_id": asset,
                    "duration_ms": facts.duration_ms,
                },
                "dialogue": [
                    {"id": "first", "speaker": "A", "english": "Hello there.", "chinese": "你好"},
                    {
                        "id": "second",
                        "speaker": "B",
                        "english": "Good morning.",
                        "chinese": "早上好",
                    },
                ],
            },
            expected_version=1,
            actor_id=actor,
        )
        auto = AutoAudioService(runtime.candidates)
        state = await auto.state(draft.id)
        assert state["status"] == "PENDING"
        runtime.candidates.asr = None
        await worker.process_stage_async(settings, state["stage_id"])
        unavailable = await auto.state(draft.id)
        assert unavailable["status"] == "FAILED"
        assert unavailable["error_code"] == "CANDIDATE_PROVIDER_UNAVAILABLE"
        assert not unavailable["candidate_id"] and asr.submissions == 0
        stage_admin = StageAdminStore(
            runtime.imports, runtime.authoring, runtime.materials, runtime.candidates
        )
        failed = await stage_admin.get(state["stage_id"])
        runtime.candidates.asr = asr
        recovered = await stage_admin.retry(
            state["stage_id"],
            actor,
            uuid4().hex,
            {
                "manual_confirmed": True,
                "expected_fence": failed["fencing_version"],
                "input_hash": failed["input_hash"],
                "config_version": failed["config_version"],
                "expected_edit_version": draft.version,
            },
        )
        assert recovered["stage_id"] == state["stage_id"]
        await worker.process_stage_async(settings, state["stage_id"])
        candidate_id = (await auto.state(draft.id))["candidate_id"]
        assert candidate_id
        await worker.process_stage_async(settings, candidate_id)
        submitted = await runtime.candidates.get(candidate_id)
        assert submitted["status"] == "SUBMITTED" and asr.submissions == 1
        # Unrelated metadata saves must not waste an already submitted paid transcription.
        revised = dict(draft.content, title_zh="问候练习")
        draft = await service.save_revision(
            draft.id, revised, expected_version=draft.version, actor_id=actor
        )
        await worker.process_stage_async(settings, submitted["stage_id"])
        aligned = await runtime.candidates.get(candidate_id)
        assert aligned["candidate"]["results"]["alignment_status"] == "ALIGNED"
        assert aligned["candidate"]["results"]["applied_edit_version"] == draft.version + 1
        assert asr.submissions == 1
        draft = await service.get_revision(draft.id)
        for sentence in ("first", "second"):
            pending = await runtime.authoring.slice_state(draft.id, sentence, draft.version)
            await worker.process_stage_async(settings, pending["stage_id"])
            draft = await service.get_revision(draft.id)
            ready = await runtime.authoring.slice_state(draft.id, sentence, draft.version)
            assert ready["status"] == "READY_TO_LISTEN" and not ready["listening_confirmed"]
            assert (
                ready["resource_version"]
                == next(row for row in draft.content["dialogue"] if row["id"] == sentence)[
                    "slice_version"
                ]
            )
            preview = await runtime.authoring.slice_preview(
                draft.id, sentence, ready["dependency_hash"]
            )
            assert preview["url"].startswith("https://private-fixture.invalid/")
        before_hash = snapshot_hash(draft.content)
        cohort = await runtime.proofreading.create(
            [
                {
                    "revision_id": draft.id,
                    "candidate_hash": before_hash,
                    "content_type": "dialogue",
                    "content_id": draft.scene_id,
                }
            ],
            actor,
        )
        assert cohort["normal_count"] == 1
        await runtime.proofreading.confirm(
            cohort["public_id"],
            draft.id,
            before_hash,
            actor,
            {"text_checked": True, "audio_listened": True},
        )
        await runtime.proofreading.complete(cohort["public_id"], actor)
        assert snapshot_hash((await service.get_revision(draft.id)).content) == before_hash
        published = await service.publish_revision(
            draft.id, actor, uuid4().hex, datetime.now(UTC), expected_version=draft.version
        )
        assert published.revision_id == draft.id
        copy = await service.create_revision(draft.scene_id, draft.id, actor, datetime.now(UTC))
        copied_stage = await auto.state(copy.id)
        assert copied_stage["status"] == "PENDING"
        await worker.process_stage_async(settings, copied_stage["stage_id"])
        assert asr.submissions == 1
        for sentence in ("first", "second"):
            pending = await runtime.authoring.slice_state(copy.id, sentence, copy.version)
            assert pending["stage_id"]
            await worker.process_stage_async(settings, pending["stage_id"])
            copy = await service.get_revision(copy.id)
            prepared = await runtime.authoring.slice_state(copy.id, sentence, copy.version)
            assert prepared["status"] == "READY_TO_LISTEN" and not prepared["listening_confirmed"]
            assert (
                prepared["resource_version"]
                != next(s for s in draft.content["dialogue"] if s["id"] == sentence)[
                    "slice_version"
                ]
            )
        assert (await service.get_scene(draft.scene_id)).published_revision_id == draft.id
        async with sessions() as session, session.begin():
            facts = await runtime.proofreading.default_facts(
                session, "dialogue", copy.id, copy.content
            )
        assert facts["ready"] and facts["normal"]
    finally:
        await runtime.client.aclose()
        await engine.dispose()
