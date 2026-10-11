from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.integrations.content_ai.aliyun import TextCandidate
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.schemas import DialogueSentence, SceneContent, SceneEntry
from juya_admin_api.modules.content.v15_candidates import (
    CandidateService,
    dependency,
    lexicon_results,
    missing_sources,
)
from juya_admin_api.modules.content.v15_candidates_router import create_candidate_router
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError, install_error_handlers


@pytest.mark.asyncio
async def test_get_restores_revision_and_actual_poll_stage_metadata():
    row = {
        "revision_id": "owned-revision",
        "status": "SUBMITTING",
        "input_hash": "input",
        "candidate": {"kind": "ASR_BOUNDARY", "stage_id": "submit", "poll_stage_id": "poll"},
    }
    candidate_result, stage_result = Mock(), Mock()
    candidate_result.mappings.return_value.first.return_value = row
    stage_result.mappings.return_value.first.return_value = {
        "public_id": "poll",
        "stage": "ASR_POLL",
        "status": "RUNNING",
        "config_version": "hash",
    }
    session = AsyncMock()
    session.execute.side_effect = [candidate_result, stage_result]
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    result = await CandidateService(sessions, Mock(), Mock()).get("candidate")
    assert result["revision_id"] == "owned-revision"
    assert result["kind"] == "ASR_BOUNDARY"
    assert result["stage_id"] == "poll" and result["stage_status"] == "RUNNING"
    assert result["stage_kind"] == "ASR_POLL" and result["config_hash"] == "hash"




def test_candidates_preserve_source_identity_and_require_ambiguous_offset_correction():
    results = lexicon_results(
        (TextCandidate("one", "bank", "河岸"), TextCandidate("two", "bank", "银行")),
        {"one": "bank bank", "two": "a bank"},
    )
    assert results[0]["start"] is None and results[0]["requires_manual_correction"]
    assert results[1]["start"] == 2 and not results[1]["requires_manual_correction"]
    overlapping = lexicon_results((TextCandidate("one", "ana", "a"),), {"one": "banana"})
    assert overlapping[0]["requires_manual_correction"]
    with pytest.raises(AppError):
        lexicon_results((TextCandidate("missing", "bank", "银行"),), {"one": "bank"})


def test_only_missing_sections_generate_and_hash_binds_whole_draft_edit_and_audio_bytes():
    content = SceneContent(
        dialogue=[
            DialogueSentence(id="one", english="one", chinese="一"),
            DialogueSentence(id="two", english="two", chinese="二"),
        ],
        vocabulary=[SceneEntry(english="one", source_sentence_ids=["one"])],
    )
    assert missing_sources(content) == {"two": "two"}
    assert dependency(content, 1, "ASR_BOUNDARY", "a") != dependency(
        content, 1, "ASR_BOUNDARY", "b"
    )
    assert dependency(content, 1, "AI_LEXICON") != dependency(content, 2, "AI_LEXICON")


@pytest.mark.asyncio
async def test_unconfigured_provider_never_generates_fixture_or_accesses_database():
    sessions = Mock()
    service = CandidateService(sessions, Mock(), Mock())
    with pytest.raises(AppError) as error:
        await service.queue("revision", "AI_LEXICON", 1, "admin", datetime.now(UTC))
    assert error.value.code == "CANDIDATE_PROVIDER_UNAVAILABLE"
    sessions.assert_not_called()


@pytest.mark.asyncio
async def test_submission_uncertainty_does_not_call_paid_provider_again():
    now = datetime.now(UTC)
    content = SceneContent(dialogue=[DialogueSentence(id="line", english="Hello", chinese="你好")])
    hashed = dependency(content, 1, "AI_LEXICON")
    row = {
        "public_id": "candidate",
        "revision_id": "revision",
        "status": "SUBMITTING",
        "candidate": {"kind": "AI_LEXICON", "sources": {"line": "Hello"}},
    }
    result = Mock()
    result.mappings.return_value.first.return_value = row
    session = AsyncMock()
    session.execute.return_value = result
    session.begin = Mock(return_value=AsyncMock())
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    stages = AsyncMock()
    stages.claim.return_value = Claim("candidate", 1, hashed, "v1", "worker")
    ai = AsyncMock()
    service = CandidateService(sessions, Mock(), stages, ai=ai)
    service._draft = AsyncMock(
        return_value=(
            SceneRevision("revision", "scene", None, content=content.model_dump(mode="json")),
            content,
        )
    )
    with pytest.raises(AppError) as error:
        await service.run("candidate", "worker", now)
    assert error.value.code == "PROVIDER_OUTCOME_UNKNOWN"
    ai.suggest.assert_not_awaited()
    stages.fail.assert_awaited_once()


def test_queue_route_is_async_and_rejects_arbitrary_text():
    service = AsyncMock()
    service.queue.return_value = "stage"

    async def admin():
        return SimpleNamespace(admin_user_id=7)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_candidate_router(service, current_admin=admin, current_admin_write=admin)
    )
    with TestClient(app) as client:
        path = "/api/v1/admin/content/revisions/revision/candidates"
        assert (
            client.post(
                path, json={"kind": "AI_LEXICON", "expected_version": 1, "text": "untrusted"}
            ).status_code
            == 422
        )
        response = client.post(path, json={"kind": "AI_LEXICON", "expected_version": 1})
        assert response.status_code == 202 and response.json()["stage_id"] == "stage"
    service.queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_asr_timestamps_remain_unmapped_unconfirmed_candidates():
    from juya_admin_api.integrations.asr.aliyun import ASRCandidate
    from juya_admin_api.modules.audio_pipeline.slicing import Boundary

    now = datetime.now(UTC)
    content = SceneContent(dialogue=[DialogueSentence(id="line", english="Hello", chinese="你好")])
    hashed = dependency(content, 1, "ASR_BOUNDARY", "sha")
    row = {
        "public_id": "candidate",
        "revision_id": "revision",
        "status": "SUBMITTED",
        "candidate": {"kind": "ASR_BOUNDARY", "poll_stage_id": "poll", "provider_task_id": "task"},
    }
    result = Mock()
    result.mappings.return_value.first.return_value = row
    session = AsyncMock()
    session.execute.return_value = result
    session.begin = Mock(return_value=AsyncMock())
    sessions = Mock(return_value=AsyncMock())
    sessions.return_value.__aenter__.return_value = session
    stages = AsyncMock()
    stages.claim.return_value = Claim("poll", 1, hashed, "v1", "worker")
    asr = AsyncMock()
    asr.poll.return_value = {
        "output": {
            "task_status": "SUCCEEDED",
            "results": [{"transcription_url": "https://allowed/result"}],
        }
    }
    asr.parse_result.return_value = ASRCandidate("Hello", (Boundary(12, 34),), False)
    production = Mock()
    service = CandidateService(sessions, production, stages, asr=asr)
    service._draft = AsyncMock(return_value=(SceneRevision("revision", "scene", None), content))
    service._audio = AsyncMock(return_value={"sha256": "sha"})
    service._save = AsyncMock()
    await service.run("poll", "worker", now)
    payload = service._save.call_args.args[1]["results"]
    assert payload["requires_manual_alignment"] and not payload["timing_confirmed"]
    assert payload["boundaries"] == [{"start_ms": 12, "end_ms": 34}]
    assert "sentence_id" not in payload["boundaries"][0]
    asr.submit.assert_not_awaited()
    assert not production.mock_calls
