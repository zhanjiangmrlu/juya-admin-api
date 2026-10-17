from unittest.mock import AsyncMock, Mock

import pytest

from juya_admin_api.modules.cloud_usage.store import CloudUsageStore
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError


def sql_session(rows):
    session = AsyncMock()
    session.scalar.return_value = 1
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    session.execute.return_value = result
    result.rowcount = 1
    return session


@pytest.mark.asyncio
async def test_only_authorized_failed_retry_creates_new_budget_attempt():
    session = sql_session([{"status": "FAILED", "attempt_number": 1}])
    claim = Claim("stage", 2, "hash", "v1", "admin")
    meter = CloudUsageStore()
    with pytest.raises(AppError):
        await meter.reserve_within(session, claim, "AI_TOKENS", 10, 100)
    await meter.reserve_within(session, claim, "AI_TOKENS", 10, 100, authorized_failed_retry=True)
    insert = session.execute.call_args_list[-1].args[0]
    assert insert.compile().params["attempt_number"] == 2
    assert insert.compile().params["reserved_units"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["UNKNOWN", "RESERVED", "SUBMITTED", "SUCCEEDED"])
async def test_unresolved_or_succeeded_attempt_never_reserves_again(status):
    session = sql_session([{"status": status, "attempt_number": 1}])
    with pytest.raises(AppError) as exc:
        await CloudUsageStore().reserve_within(
            session,
            Claim("stage", 2, "hash", "v1", "admin"),
            "AI_TOKENS",
            10,
            100,
            authorized_failed_retry=True,
        )
    assert exc.value.code == "PROVIDER_OUTCOME_UNKNOWN"
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_retry_rejects_unknown_candidate_before_reserving_or_dispatching():
    stage = {
        "status": "FAILED",
        "fencing_version": 2,
        "input_hash": "hash",
        "config_version": "v1",
        "stage": "AI_LEXICON",
    }
    candidate = {
        "public_id": "candidate",
        "status": "UNKNOWN",
        "revision_id": "r",
        "candidate": {"kind": "AI_LEXICON"},
    }
    session = AsyncMock()
    results = []
    for row in (stage, candidate):
        result = Mock()
        result.mappings.return_value.first.return_value = row
        results.append(result)
    session.execute.side_effect = results
    session.begin = Mock(return_value=AsyncMock())
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    usage = AsyncMock()
    production = Mock()
    production.creation_replay = AsyncMock(return_value=None)
    service = CandidateService(sessions, production, Mock(), usage=usage)
    with pytest.raises(AppError) as exc:
        await service.retry(
            "stage", 2, "hash", "v1", 1, "admin", "key", acknowledged_charged_retry=True
        )
    assert exc.value.code == "PROVIDER_OUTCOME_UNKNOWN"
    usage.reserve_within.assert_not_awaited()
