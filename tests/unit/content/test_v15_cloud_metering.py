import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from juya_admin_api.integrations.content_ai.aliyun import AliyunContentAI
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_provider_result_binds_candidates_and_usage_without_shared_mutable_state():
    def respond(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 32
        return httpx.Response(
            200,
            json={
                "id": "request1",
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidates": [
                                        {"source_id": "s", "english": "hello", "chinese": "你好"}
                                    ]
                                }
                            )
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AliyunContentAI(client, "fixture-key")
        result = await provider.suggest({"s": "hello"}, max_output_tokens=32)
    assert result.candidates[0].english == "hello"
    assert result.usage["total_tokens"] == 9
    assert result.request_id == "request1"
    assert not hasattr(provider, "last_usage")


@pytest.mark.asyncio
async def test_paid_candidates_default_budget_off_before_database():
    from datetime import UTC, datetime

    sessions = Mock()
    service = CandidateService(sessions, Mock(), Mock(), ai=AsyncMock())
    with pytest.raises(AppError) as caught:
        await service.queue("revision", "AI_LEXICON", 1, "admin", datetime.now(UTC))
    assert caught.value.code == "CLOUD_BUDGET_DISABLED"
    sessions.assert_not_called()


@pytest.mark.asyncio
async def test_lost_claim_cannot_write_candidate_or_billing():
    session = AsyncMock()
    session.begin = Mock(return_value=AsyncMock())
    session.scalar.return_value = None
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    usage = AsyncMock()
    service = CandidateService(sessions, Mock(), Mock(), usage=usage)
    with pytest.raises(AppError) as caught:
        await service._save(
            "candidate",
            {"usage_attempt_id": "attempt"},
            "UNKNOWN",
            Claim("stage", 1, "hash", "v1", "old-worker"),
            {"status": "UNKNOWN"},
        )
    assert caught.value.code == "IMPORT_STAGE_LEASE_LOST"
    session.execute.assert_not_awaited()
    usage.record_within.assert_not_awaited()


@pytest.mark.asyncio
async def test_asr_poll_exhaustion_does_not_resubmit_or_schedule_more_polls():
    from datetime import UTC, datetime

    stages = AsyncMock()
    stages.claim.return_value = Claim("poll-stage", 1, "hash", "v1", "worker")
    asr = AsyncMock()
    asr.poll.return_value = {"output": {"task_status": "RUNNING"}}
    service = CandidateService(Mock(), Mock(), stages, asr=asr, max_asr_polls=2)
    payload = {
        "kind": "ASR_BOUNDARY",
        "provider_task_id": "known-paid-task",
        "usage_attempt_id": "paid-attempt",
        "poll_count": 1,
    }
    service._initialize = AsyncMock(return_value=("candidate", payload, Mock(), {}, True))
    service._save = AsyncMock()
    with pytest.raises(AppError) as caught:
        await service.run("poll-stage", "worker", datetime.now(UTC))
    assert caught.value.code == "ASR_POLL_LIMIT"
    assert payload["poll_count"] == 2
    assert stages.fail.call_args.args[-1] is False
    asr.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_asr_permanent_failed_result_is_not_automatically_polled_again():
    from datetime import UTC, datetime

    stages = AsyncMock()
    stages.claim.return_value = Claim("poll-stage", 1, "hash", "v1", "worker")
    asr = AsyncMock()
    asr.poll.return_value = {"output": {"task_status": "FAILED"}}
    service = CandidateService(Mock(), Mock(), stages, asr=asr)
    payload = {
        "kind": "ASR_BOUNDARY",
        "provider_task_id": "known-paid-task",
        "usage_attempt_id": "paid-attempt",
    }
    service._initialize = AsyncMock(return_value=("candidate", payload, Mock(), {}, True))
    service._save = AsyncMock()
    with pytest.raises(AppError):
        await service.run("poll-stage", "worker", datetime.now(UTC))
    assert service._save.call_args.args[2] == "FAILED"
    assert stages.fail.call_args.args[-1] is False
    asr.submit.assert_not_awaited()
