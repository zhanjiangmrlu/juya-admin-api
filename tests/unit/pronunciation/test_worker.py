from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.pronunciation.domain import (
    AudioResult,
    Config,
    SynthesisFailure,
    Target,
)
from juya_admin_api.modules.pronunciation.tasks import PronunciationWorker


def dependencies(attempt=0, audio=None):
    target = Target("target", "hello")
    job = {
        "public_id": "job",
        "cache_key": "key",
        "target_id": "target",
        "lease_token": "lease",
        "attempt": attempt,
        "audio": audio,
        "target_snapshot": asdict(target),
        "config": asdict(Config(enabled=True, price_version="20261011")),
    }
    repository = SimpleNamespace(
        claim=AsyncMock(return_value=job),
        target=AsyncMock(return_value=target),
        reserve=AsyncMock(return_value="attempt"),
        fenced_update=AsyncMock(),
        finish_attempt=AsyncMock(),
        complete=AsyncMock(),
        task=AsyncMock(return_value=job),
    )
    provider = SimpleNamespace(generate=AsyncMock(return_value=AudioResult(b"audio", "request", 5)))
    oss = SimpleNamespace(freeze_bytes=AsyncMock(side_effect=RuntimeError("upload down")))
    security = SimpleNamespace(scan_audio=AsyncMock(), poll_audio=AsyncMock())
    return job, repository, provider, oss, security


@pytest.mark.asyncio
async def test_unknown_timeout_never_schedules_a_paid_retry():
    _job, repository, provider, oss, security = dependencies()
    provider.generate.side_effect = SynthesisFailure("TIMEOUT", unknown=True, retryable=True)
    await PronunciationWorker(repository, provider, oss, security).run("job")
    values = repository.fenced_update.call_args.kwargs
    assert values["status"] == "UNKNOWN"
    assert values["outbox_pending"] is False
    assert repository.reserve.await_count == 1


@pytest.mark.asyncio
async def test_at_most_two_automatic_retries():
    _job, repository, provider, oss, security = dependencies(attempt=2)
    provider.generate.side_effect = SynthesisFailure("LIMIT", retryable=True)
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert repository.fenced_update.call_args.kwargs["status"] == "FAILED"


@pytest.mark.asyncio
async def test_upload_failure_recovery_uses_retained_paid_audio(monkeypatch):
    _job, repository, provider, oss, security = dependencies(audio=b"paid-audio")
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(return_value=SimpleNamespace(content_type="audio/mpeg", duration_ms=20)),
    )
    await PronunciationWorker(repository, provider, oss, security).run("job")
    provider.generate.assert_not_awaited()
    repository.reserve.assert_not_awaited()
    assert repository.fenced_update.call_args.kwargs["status"] == "RECOVER"


@pytest.mark.asyncio
async def test_budget_failure_prevents_cloud_call():
    from juya_admin_api.shared.errors import AppError

    _job, repository, provider, oss, security = dependencies()
    repository.reserve.side_effect = AppError("TTS_BUDGET_EXHAUSTED", "budget", 429)
    await PronunciationWorker(repository, provider, oss, security).run("job")
    provider.generate.assert_not_awaited()
    assert repository.fenced_update.call_args.kwargs["error_code"] == "TTS_BUDGET_EXHAUSTED"
