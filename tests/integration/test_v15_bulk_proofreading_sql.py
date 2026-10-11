"""Real SQL: sample-only manual review unlocks explicit prepared-normal batch confirmation."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_normal_batch_keeps_abnormal_draft_and_replays_complete():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    proof = ProofreadingStore(sessions)
    short = ShortReadingStore(sessions)
    try:
        series = await ProductionStore(sessions).create_series(
            "Normal review batch", uuid4().hex, None
        )
        content = dict(
            title_en="Read",
            title_zh="阅读",
            category="Daily",
            topic="Life",
            level="A1",
            safe_intro="A day",
            paragraphs_en=["Complete English."],
            translation_zh="完整中文",
            copyright_status="CONFIRMED",
            copyright_source="Owned fixture",
        )
        candidates = []
        for index in range(4):
            created = await short.command(
                "create",
                "new",
                "test",
                uuid4().hex,
                {
                    "series_id": series["id"],
                    "content": content if index < 3 else {**content, "title_zh": ""},
                },
            )
            async with sessions() as session:
                digest = await session.scalar(
                    text("SELECT content_hash FROM short_reading_revision WHERE public_id=:id"),
                    {"id": created["revision_id"]},
                )
            candidates.append(
                dict(
                    content_type="short_reading",
                    content_id=created["content_id"],
                    revision_id=created["revision_id"],
                    candidate_hash=digest,
                    normal=True,
                )
            )
        cohort = await proof.create(candidates, "test")
        assert cohort["normal_count"] == 3 and len(cohort["sample_ids"]) == 1
        abnormal = candidates[-1]
        frozen = {candidate["revision_id"]: candidate for candidate in cohort["candidates"]}
        assert frozen[abnormal["revision_id"]]["normal"] is False  # Client normal=True is ignored.
        normals = [
            candidate["revision_id"] for candidate in cohort["candidates"] if candidate["normal"]
        ]
        key = uuid4().hex
        with pytest.raises(AppError) as not_sampled:
            await proof.bulk_confirm(
                cohort["public_id"], "test", cohort["collection_hash"], normals, True, key
            )
        assert not_sampled.value.code == "PROOFREADING_SAMPLE_REQUIRED"
        sampled = frozen[cohort["sample_ids"][0]]
        await proof.confirm(
            cohort["public_id"],
            sampled["revision_id"],
            sampled["candidate_hash"],
            "test",
            {"text_checked": True},
        )
        with pytest.raises(AppError) as abnormal_batch:
            await proof.bulk_confirm(
                cohort["public_id"],
                "test",
                cohort["collection_hash"],
                [abnormal["revision_id"]],
                True,
                uuid4().hex,
            )
        assert abnormal_batch.value.code == "PROOFREADING_ABNORMAL_REQUIRES_MANUAL"
        result = await proof.bulk_confirm(
            cohort["public_id"], "test", cohort["collection_hash"], normals, True, key
        )
        assert set(result["confirmed_ids"]) == set(normals)
        async with sessions() as session:
            methods = (
                (
                    await session.execute(
                        text("SELECT method FROM proofreading_confirmation WHERE set_id=:id"),
                        {"id": cohort["public_id"]},
                    )
                )
                .scalars()
                .all()
            )
            assert methods.count("MANUAL") == 1 and methods.count("BATCH_NORMAL") == 2
        with pytest.raises(AppError):
            await proof.complete(cohort["public_id"], "test")
        await proof.complete(cohort["public_id"], "test", allow_abnormal_drafts=True)
        assert (await proof.get(cohort["public_id"]))["status"] == "CONFIRMED"
        assert (
            await proof.bulk_confirm(
                cohort["public_id"], "test", cohort["collection_hash"], normals, True, key
            )
            == result
        )
        assert (await proof.get(cohort["public_id"]))["status"] == "CONFIRMED"
        async with sessions() as session:
            assert await proof.is_confirmed_within(
                session, sampled["revision_id"], sampled["candidate_hash"]
            )
            assert not await proof.is_confirmed_within(
                session, abnormal["revision_id"], abnormal["candidate_hash"]
            )
        with pytest.raises(AppError):
            await proof.bulk_confirm(
                cohort["public_id"],
                "test",
                cohort["collection_hash"],
                [sampled["revision_id"]],
                True,
                key,
            )
    finally:
        await engine.dispose()
