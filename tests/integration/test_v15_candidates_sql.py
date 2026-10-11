import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.integrations.content_ai.aliyun import ContentAIResult, TextCandidate
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_ai_candidate_queue_current_dependencies_and_manual_adoption():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProductionStore(sessions)
    content = ContentService(SQLAlchemyContentRepository(sessions))
    stages = StageStore(sessions)
    ai = AsyncMock()
    ai.model = 'fixture-qwen-flash'
    ai.suggest.return_value = ContentAIResult(
        (TextCandidate("line", "bank", "河岸"),),
        {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        "fixture-request",
    )
    service = CandidateService(sessions, store, stages, ai=ai, content_ai_daily_token_limit=100000)
    now = datetime.now(UTC)
    series = await store.create_series("candidate fixture", "candidate-" + uuid4().hex, None)
    scene = await store.create_scene(series["id"], "dialogue")
    revision = await content.create_revision(scene, None, "test", now)
    try:
        revision = await content.save_revision(
            revision.id,
            {
                "schema_version": 2,
                "dialogue": [
                    {"id": "line", "english": "a bank by the bank", "chinese": "岸边的银行"}
                ],
            },
            expected_version=revision.version,
            actor_id="test",
        )
        identifier = await service.queue(revision.id, "AI_LEXICON", revision.version, "test", now)
        assert (
            await service.queue(revision.id, "AI_LEXICON", revision.version, "test", now)
            == identifier
        )
        assert await service.run(identifier, "worker", datetime.now(UTC)) == {
            "candidate_id": identifier
        }
        ai.suggest.assert_awaited_once_with({"line": "a bank by the bank"}, max_output_tokens=4096)
        candidate = await service.get(identifier)
        assert candidate["candidate"]["results"][0]["requires_manual_correction"]
        current = await content.get_revision(revision.id)
        assert not current.content.get("vocabulary")
        with pytest.raises(AppError):
            await service.adopt(
                identifier, 0, "wrong", 2, 6, "河岸", current.version, "test", datetime.now(UTC)
            )
        adopted = await service.adopt(
            identifier, 0, "line", 2, 6, "河岸", current.version, "test", datetime.now(UTC)
        )
        assert len(adopted.content["vocabulary"]) == 1
        entry = adopted.content["vocabulary"][0]
        assert entry["entry_id"] and entry["source_id"] and entry["sense_id"]
        assert entry["source_locator"]["start"] == "2"
        assert (await service.get(identifier))["status"] == "ADOPTED"
        with pytest.raises(AppError):
            await service.adopt(
                identifier, 0, "line", 2, 6, "河岸", current.version, "test", datetime.now(UTC)
            )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM content_cloud_attempt WHERE stage_id IN "
                    "(SELECT public_id FROM content_import_stage WHERE item_id=:revision)"
                ),
                {"revision": revision.id},
            )
            await session.execute(
                text(
                    "DELETE FROM admin_outbox WHERE aggregate_public_id IN "
                    "(SELECT public_id FROM content_import_stage WHERE item_id=:revision)"
                ),
                {"revision": revision.id},
            )
            await session.execute(
                text("DELETE FROM content_import_stage WHERE item_id=:revision"),
                {"revision": revision.id},
            )
            await session.execute(
                text("DELETE FROM lexicon_candidate WHERE revision_id=:revision"),
                {"revision": revision.id},
            )
            await session.execute(
                text(
                    "UPDATE scene SET draft_revision_id=NULL,published_revision_id=NULL "
                    "WHERE public_id=:id"
                ),
                {"id": scene},
            )
            await session.execute(text("DELETE FROM scene WHERE public_id=:id"), {"id": scene})
            await session.execute(
                text("DELETE FROM content_series WHERE public_id=:id"), {"id": series["id"]}
            )
        await engine.dispose()
