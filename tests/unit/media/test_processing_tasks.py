from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.ocr.protocol import OcrResult
from juya_admin_api.integrations.tts.protocol import TtsResult
from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService
from juya_admin_api.modules.media.tasks import (
    InMemoryMediaJobRepository,
    MediaTaskService,
    PersistentMediaTaskService,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_concurrent_redelivery_claims_provider_once_even_after_failure() -> None:
    import asyncio

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)

    class SlowFailure:
        calls = 0

        async def recognize(self, object_key: str, template_type: str) -> OcrResult:
            self.calls += 1
            await asyncio.sleep(0.01)
            raise RuntimeError("timeout")

    ocr = SlowFailure()
    worker = PersistentMediaTaskService(
        admin,
        repository,
        ocr,
        CountingTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="concurrent-ocr",
        job_type="OCR",
        target_id="asset",
        actor_id="admin-1",
        now=NOW,
    )
    await asyncio.gather(
        *(worker.run_ocr(job.id, "uploads/images/admin-1/a.png", "dialogue", NOW) for _ in range(3))
    )
    await worker.run_ocr(job.id, "uploads/images/admin-1/a.png", "dialogue", NOW)
    assert ocr.calls == 1
    assert (await admin.get_job(job.id)).status == "FAILED"


class FailingOcr:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        raise RuntimeError(f"provider unavailable: {object_key}:{template_type}")


class FakeTts:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        del voice, text
        return TtsResult("provider-1", f"generated/{audio_target}.mp3", 1200)


class CountingOcr:
    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        self.calls += 1
        return OcrResult(
            f"ocr-provider-{self.calls}",
            f"recognized:{object_key}:{template_type}",
            [{"type": "title", "text": "Coffee time", "confidence": 0.98}],
        )


class CountingTts:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        self.calls += 1
        return TtsResult(
            f"tts-provider-{self.calls}",
            f"generated/audio/{audio_target}/{voice}/{self.calls}.mp3",
            len(text) * 100,
        )


@pytest.mark.asyncio
async def test_provider_failure_does_not_overwrite_manual_audio_revision() -> None:
    repository = InMemoryMediaJobRepository()
    repository.set_manual_audio("target-1", "manual/revision-2.mp3", revision=2)
    service = MediaTaskService(repository, FailingOcr(), FakeTts())

    result = await service.run_ocr(
        "ocr:asset-1", "asset-1", "uploads/images/admin-1/image.png", NOW
    )

    assert result.status == "FAILED"
    assert repository.audio_targets["target-1"].object_key == "manual/revision-2.mp3"
    assert repository.audio_targets["target-1"].source == "MANUAL"


@pytest.mark.asyncio
async def test_batch_items_fail_independently_and_job_key_is_idempotent() -> None:
    repository = InMemoryMediaJobRepository()
    service = MediaTaskService(repository, FailingOcr(), FakeTts())

    first = await service.run_tts_batch(
        "tts:batch-1",
        (
            ("target-1", "hello", "voice-a"),
            ("target-2", "", "voice-a"),
            ("target-3", "world", "voice-a"),
        ),
        NOW,
    )
    replay = await service.run_tts_batch("tts:batch-1", (("target-4", "ignored", "voice-a"),), NOW)

    assert first is replay
    assert (first.total_count, first.success_count, first.failure_count) == (3, 0, 3)
    assert repository.audio_targets == {}


@pytest.mark.asyncio
async def test_persistent_ocr_redelivery_does_not_repeat_provider_or_candidate() -> None:
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    ocr = CountingOcr()
    worker = PersistentMediaTaskService(
        admin,
        repository,
        ocr,
        CountingTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="ocr:asset-1",
        job_type="OCR",
        target_id="asset-1",
        actor_id="admin-1",
        now=NOW,
    )

    first = await worker.run_ocr(
        job.id,
        object_key="uploads/images/admin-1/image.png",
        template_type="learning-card",
        now=NOW,
    )
    replayed = await worker.run_ocr(
        job.id,
        object_key="uploads/images/admin-1/image.png",
        template_type="learning-card",
        now=NOW,
    )

    assert first.status == replayed.status == "SUCCEEDED"
    assert ocr.calls == 1
    assert len(repository.ocr_candidates) == 1


@pytest.mark.asyncio
async def test_cancelled_job_never_calls_provider_and_cannot_be_completed_by_redelivery() -> None:
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    ocr = CountingOcr()
    worker = PersistentMediaTaskService(
        admin,
        repository,
        ocr,
        CountingTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="ocr:asset-2",
        job_type="OCR",
        target_id="asset-2",
        actor_id="admin-1",
        now=NOW,
    )
    await admin.cancel_job(job.id, now=NOW)

    result = await worker.run_ocr(
        job.id,
        object_key="uploads/images/admin-1/cancelled.png",
        template_type="learning-card",
        now=NOW,
    )

    assert result.status == "CANCELLED"
    assert ocr.calls == 0
    assert repository.ocr_candidates == {}


@pytest.mark.asyncio
async def test_tts_redelivery_is_disabled_without_replacing_manual_active() -> None:
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    tts = CountingTts()
    manual = await admin.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="manual-asset",
        source="MANUAL",
        actor_id="admin-1",
        now=NOW,
    )
    target = await admin.confirm_audio_version(manual.id, actor_id="admin-1", now=NOW)
    job = await admin.create_job(
        business_key="tts:sentence-1",
        job_type="TTS",
        target_id=target.id,
        actor_id="admin-1",
        now=NOW,
    )
    worker = PersistentMediaTaskService(
        admin,
        repository,
        CountingOcr(),
        tts,
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )

    await worker.run_tts(
        job.id,
        stable_key="sentence-1",
        target_type="SENTENCE",
        text="Hello",
        voice="en-US-1",
        now=NOW,
    )
    await worker.run_tts(
        job.id,
        stable_key="sentence-1",
        target_type="SENTENCE",
        text="Hello",
        voice="en-US-1",
        now=NOW,
    )

    refreshed = await admin.get_audio_target(target.id)
    versions = await admin.list_audio_versions(target.id)
    assert refreshed.active_version_id == manual.id
    assert tts.calls == 0
    assert len(versions) == 1
    assert versions[-1].source == "MANUAL"
    assert versions[-1].status == "ACTIVE"


async def _asset_id(object_key: str, now: datetime) -> str:
    del now
    return f"asset:{object_key}"


@pytest.mark.asyncio
async def test_persistent_ocr_cannot_access_another_actor_prefix() -> None:
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    ocr = CountingOcr()
    worker = PersistentMediaTaskService(
        admin,
        repository,
        ocr,
        CountingTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="ocr:foreign", job_type="OCR", target_id="asset", actor_id="admin-1", now=NOW
    )
    result = await worker.run_ocr(job.id, "uploads/images/admin-2/a.png", "default", NOW)
    assert result.status == "FAILED"
    assert result.error_code == "OCR_OBJECT_INVALID"
    assert ocr.calls == 0


@pytest.mark.asyncio
async def test_tts_registration_failure_is_terminal_and_sanitized() -> None:
    async def fail(result: TtsResult, now: datetime) -> str:
        raise RuntimeError("https://private.test/?x-oss-signature=private-secret")

    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    worker = PersistentMediaTaskService(
        admin, repository, CountingOcr(), CountingTts(), register_generated_audio=fail
    )
    job = await admin.create_job(
        business_key="tts:fail", job_type="TTS", target_id="target", actor_id="admin-1", now=NOW
    )
    result = await worker.run_tts(
        job.id, stable_key="sentence", target_type="SENTENCE", text="Hello", voice="en", now=NOW
    )
    assert result.status == "FAILED"
    assert result.error_code == "TTS_DISABLED"
    assert "private-secret" not in repr(result)


@pytest.mark.asyncio
async def test_tts_output_outside_generated_audio_is_rejected() -> None:
    repository = InMemoryMediaAdminRepository()
    admin = MediaAdminService(repository)
    worker = PersistentMediaTaskService(
        admin,
        repository,
        CountingOcr(),
        FakeTts(),
        register_generated_audio=lambda result, now: _asset_id(result.object_key, now),
    )
    job = await admin.create_job(
        business_key="tts:bad-key", job_type="TTS", target_id="target", actor_id="admin-1", now=NOW
    )
    result = await worker.run_tts(
        job.id, stable_key="sentence", target_type="SENTENCE", text="Hello", voice="en", now=NOW
    )
    assert result.status == "FAILED"
    assert result.error_code == "TTS_DISABLED"
    assert repository.audio_versions == {}
