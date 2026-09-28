from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.ocr.protocol import OcrResult
from juya_admin_api.integrations.tts.protocol import TtsResult
from juya_admin_api.modules.media.tasks import (
    InMemoryMediaJobRepository,
    MediaTaskService,
)

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class FailingOcr:
    async def recognize(self, object_key: str, template_type: str) -> OcrResult:
        raise RuntimeError(f"provider unavailable: {object_key}:{template_type}")


class FakeTts:
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult:
        del voice, text
        return TtsResult("provider-1", f"generated/{audio_target}.mp3", 1200)


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
    assert (first.total_count, first.success_count, first.failure_count) == (3, 2, 1)
    assert repository.audio_targets["target-1"].source == "TTS"
    assert repository.audio_targets["target-3"].source == "TTS"
