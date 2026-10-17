import asyncio
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
async def test_explicit_paid_retry_reserves_once_preserves_old_meter_and_is_idempotent():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL including 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    production, stages = ProductionStore(sessions), StageStore(sessions)
    content = ContentService(SQLAlchemyContentRepository(sessions))
    ai = AsyncMock()
    ai.model = "fixture-qwen"
    ai.suggest.return_value = ContentAIResult(
        (TextCandidate("line", "bank", "岸"),),
        {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "fixture",
    )
    service = CandidateService(
        sessions, production, stages, ai=ai, content_ai_daily_token_limit=1000000
    )
    now = datetime.now(UTC)
    series = await production.create_series("manual retry fixture", "retry-" + uuid4().hex, None)
    scene = await production.create_scene(series["id"], "dialogue")
    revision = await content.create_revision(scene, None, "test", now)
    try:
        revision = await content.save_revision(
            revision.id,
            {
                "schema_version": 2,
                "dialogue": [{"id": "line", "english": "a bank", "chinese": "河岸"}],
            },
            expected_version=revision.version,
            actor_id="test",
        )
        stage_id = await service.queue(revision.id, "AI_LEXICON", revision.version, "admin", now)
        claim = await stages.claim(stage_id, "first-worker", datetime.now(UTC))
        async with sessions() as session, session.begin():
            old_attempt = await service.usage.reserve_within(
                session, claim, "AI_TOKENS", 10000, 1000000
            )
            await service.usage.record_within(
                session, claim, old_attempt, "FAILED", actual_units=7, usage={"known_failure": True}
            )
            await session.execute(
                text("UPDATE lexicon_candidate SET status='FAILED' WHERE public_id=:id"),
                {"id": stage_id},
            )
        await stages.fail(claim, "KNOWN_PROVIDER_FAILURE", datetime.now(UTC), False)
        arguments = (
            stage_id,
            claim.fencing_version,
            claim.input_hash,
            claim.config_version,
            revision.version,
            "admin",
            "retry-key",
        )
        with pytest.raises(AppError) as exc:
            await service.retry(*arguments)
        assert exc.value.code == "CHARGED_RETRY_ACK_REQUIRED"
        service.content_ai_daily_token_limit = 0
        with pytest.raises(AppError) as exc:
            await service.retry(*arguments, acknowledged_charged_retry=True)
        assert exc.value.code == "CLOUD_BUDGET_DISABLED"
        service.content_ai_daily_token_limit = 1
        with pytest.raises(AppError) as exc:
            await service.retry(*arguments, acknowledged_charged_retry=True)
        assert exc.value.code == "CLOUD_BUDGET_EXHAUSTED"
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM content_import_stage WHERE public_id=:id"),
                    {"id": stage_id},
                )
                == "FAILED"
            )
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM content_cloud_attempt WHERE stage_id=:id"),
                    {"id": stage_id},
                )
                == 1
            )
        service.content_ai_daily_token_limit = 1000000
        results = await asyncio.gather(
            *[service.retry(*arguments, acknowledged_charged_retry=True) for _ in range(3)]
        )
        assert sum(not item["replayed"] for item in results) == 1
        result = next(item for item in results if not item["replayed"])
        assert result["status"] == "QUEUED" and result["paid_retry"]
        assert (await service.retry(*arguments, acknowledged_charged_retry=True))["replayed"]
        ai.suggest.assert_not_awaited()
        async with sessions() as session:
            attempts = (
                (
                    await session.execute(
                        text(
                            "SELECT attempt_number,status,actual_units FROM content_cloud_attempt "
                            "WHERE stage_id=:id ORDER BY attempt_number"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .all()
            )
        assert [
            (row["attempt_number"], row["status"], row["actual_units"]) for row in attempts
        ] == [(1, "FAILED", 7), (2, "RESERVED", None)]
        await service.run(stage_id, "retry-worker", datetime.now(UTC))
        ai.suggest.assert_awaited_once()
        async with sessions() as session:
            attempts = (
                (
                    await session.execute(
                        text(
                            "SELECT attempt_number,status,actual_units FROM content_cloud_attempt "
                            "WHERE stage_id=:id ORDER BY attempt_number"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .all()
            )
        assert [(row["status"], row["actual_units"]) for row in attempts] == [
            ("FAILED", 7),
            ("SUCCEEDED", 8),
        ]
    finally:
        async with sessions() as session, session.begin():
            for query in (
                "DELETE FROM audit_event WHERE object_public_id IN "
                "(SELECT public_id FROM content_import_stage WHERE item_id=:revision)",
                "DELETE FROM idempotency_record WHERE scope IN "
                "(SELECT CONCAT('v15.candidate.retry:',public_id) FROM content_import_stage "
                "WHERE item_id=:revision)",
                "DELETE FROM content_cloud_attempt WHERE stage_id IN "
                "(SELECT public_id FROM content_import_stage WHERE item_id=:revision)",
                "DELETE FROM admin_outbox WHERE aggregate_public_id IN "
                "(SELECT public_id FROM content_import_stage WHERE item_id=:revision)",
                "DELETE FROM content_import_stage WHERE item_id=:revision",
                "DELETE FROM lexicon_candidate WHERE revision_id=:revision",
            ):
                await session.execute(text(query), {"revision": revision.id})
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
