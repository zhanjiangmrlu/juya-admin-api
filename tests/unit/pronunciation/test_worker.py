from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.integrations.content_security.protocol import SecurityResult
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
        "security_status": None,
        "security_request_id": None,
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
async def test_pending_audio_review_is_persisted_and_resumed_without_paid_resubmission(monkeypatch):
    job, repository, provider, oss, security = dependencies()
    provider.generate.return_value = AudioResult(b"paid-audio", "tencent-request", 5)
    oss.freeze_bytes.side_effect = None
    oss.freeze_bytes.return_value = "sealed/media/audio/recovery.mp3"
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(
            return_value=SimpleNamespace(
                content_type="audio/mpeg", duration_ms=20, sha256="a" * 64, size=10
            )
        ),
    )
    repository.claim.side_effect = lambda *_: dict(job)

    async def persist(_claim, **values):
        job.update(values)

    repository.fenced_update.side_effect = persist
    repository.task.side_effect = lambda *_: dict(job)
    security.scan_audio.return_value = SecurityResult("green-task", "PENDING")
    security.poll_audio.side_effect = [
        SecurityResult("green-task", "PENDING"),
        SecurityResult("green-task", "PASSED"),
    ]
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert job["status"] == "RECOVER"
    assert job["error_code"] == "AUDIO_SECURITY_PENDING"
    assert job["security_request_id"] == "green-task"
    assert job["security_status"] == "PENDING"
    assert job["request_id"] == "tencent-request"
    repository.complete.assert_not_awaited()

    # A fresh worker reads the persisted checkpoint rather than its previous in-memory scan.
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert repository.complete.call_args.kwargs["security_request_id"] == "green-task"
    assert job["security_status"] == "PASSED"
    assert job["request_id"] == "tencent-request"
    assert provider.generate.await_count == repository.reserve.await_count == 1
    assert oss.freeze_bytes.await_count == security.scan_audio.await_count == 1
    assert [call.args[0] for call in security.poll_audio.await_args_list] == [
        "green-task",
        "green-task",
    ]


@pytest.mark.asyncio
async def test_unknown_audio_review_submission_cannot_be_automatically_repeated(monkeypatch):
    job, repository, provider, oss, security = dependencies(audio=b"paid-audio")
    job.update(object_key="sealed/media/audio/recovery.mp3", security_status="SUBMITTING")
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(
            return_value=SimpleNamespace(
                content_type="audio/mpeg", duration_ms=20, sha256="a" * 64, size=10
            )
        ),
    )
    await PronunciationWorker(repository, provider, oss, security).run("job")
    values = repository.fenced_update.call_args.kwargs
    assert values["status"] == "UNKNOWN"
    assert values["error_code"] == "AUDIO_SECURITY_UNKNOWN"
    assert values["outbox_pending"] is False
    security.scan_audio.assert_not_awaited()
    security.poll_audio.assert_not_awaited()
    provider.generate.assert_not_awaited()
    repository.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_submission_requires_a_current_fenced_checkpoint(monkeypatch):
    from juya_admin_api.shared.errors import AppError

    job, repository, provider, oss, security = dependencies(audio=b"paid-audio")
    job["object_key"] = "sealed/media/audio/recovery.mp3"
    repository.fenced_update.side_effect = AppError("LEASE_LOST", "lost", 409)
    security.scan_audio.return_value = SecurityResult("green-task", "PASSED")
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(
            return_value=SimpleNamespace(
                content_type="audio/mpeg", duration_ms=20, sha256="a" * 64, size=10
            )
        ),
    )
    await PronunciationWorker(repository, provider, oss, security).run("job")
    security.scan_audio.assert_not_awaited()
    provider.generate.assert_not_awaited()
    repository.complete.assert_not_awaited()
    repository.reserve.assert_not_awaited()
    assert repository.fenced_update.call_args.kwargs == {"security_status": "SUBMITTING"}


@pytest.mark.asyncio
async def test_budget_failure_prevents_cloud_call():
    from juya_admin_api.shared.errors import AppError

    _job, repository, provider, oss, security = dependencies()
    repository.reserve.side_effect = AppError("TTS_BUDGET_EXHAUSTED", "budget", 429)
    await PronunciationWorker(repository, provider, oss, security).run("job")
    provider.generate.assert_not_awaited()
    assert repository.fenced_update.call_args.kwargs["error_code"] == "TTS_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_ready_registration_receives_actual_inspection_and_security_facts(monkeypatch):
    _job, repository, provider, oss, security = dependencies(audio=b"paid-audio")
    inspected = SimpleNamespace(content_type="audio/mpeg", duration_ms=20, sha256="a" * 64, size=10)
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(return_value=inspected),
    )
    oss.freeze_bytes.side_effect = None
    oss.freeze_bytes.return_value = "sealed/media/audio/audio.mp3"
    security.scan_audio.return_value = SimpleNamespace(
        status="PASSED", provider_request_id="security-id"
    )
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert repository.complete.call_args.kwargs == {
        "sha256": "a" * 64,
        "size_bytes": 10,
        "content_type": "audio/mpeg",
        "security_status": "PASSED",
        "security_request_id": "security-id",
    }
    provider.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_passed_review_survives_local_completion_failure_without_polling_again(monkeypatch):
    job, repository, provider, oss, security = dependencies()
    provider.generate.return_value = AudioResult(b"paid-audio", "tencent-request", 5)
    oss.freeze_bytes.side_effect = None
    oss.freeze_bytes.return_value = "sealed/media/audio/recovery.mp3"
    monkeypatch.setattr(
        "juya_admin_api.modules.pronunciation.tasks.inspect_media",
        AsyncMock(
            return_value=SimpleNamespace(
                content_type="audio/mpeg", duration_ms=20, sha256="a" * 64, size=10
            )
        ),
    )
    repository.claim.side_effect = lambda *_: dict(job)

    async def persist(_claim, **values):
        job.update(values)

    repository.fenced_update.side_effect = persist
    repository.task.side_effect = lambda *_: dict(job)
    repository.complete.side_effect = [RuntimeError("local completion unavailable"), None]
    security.scan_audio.return_value = SecurityResult("green-task", "PENDING")
    security.poll_audio.return_value = SecurityResult("green-task", "PASSED")
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert job["status"] == "RECOVER" and job["error_code"] == "LOCAL_STAGE_FAILED"
    assert job["audio"] == b"paid-audio"
    assert job["security_status"] == "PASSED" and job["security_request_id"] == "green-task"
    assert job["request_id"] == "tencent-request"
    await PronunciationWorker(repository, provider, oss, security).run("job")
    assert repository.complete.await_count == 2
    assert repository.complete.call_args.kwargs["security_request_id"] == "green-task"
    assert repository.complete.call_args.kwargs["security_status"] == "PASSED"
    assert provider.generate.await_count == security.scan_audio.await_count == 1
    assert security.poll_audio.await_count == 1
