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
async def test_frozen_set_replay_append_failure_and_changed_hash():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProofreadingStore(sessions)
    try:
        series = await ProductionStore(sessions).create_series("Proofreading", uuid4().hex, None)
        candidates = []
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
        short = ShortReadingStore(sessions)
        for _ in range(2):
            created = await short.command(
                "create",
                "new",
                "test",
                uuid4().hex,
                {"series_id": series["id"], "content": content},
            )
            async with sessions() as session:
                digest = await session.scalar(
                    text("SELECT content_hash FROM short_reading_revision WHERE public_id=:id"),
                    {"id": created["revision_id"]},
                )
            candidates.append(
                {
                    "content_type": "short_reading",
                    "content_id": created["content_id"],
                    "revision_id": created["revision_id"],
                    "candidate_hash": digest,
                }
            )
        set_id = uuid4().hex
        original = await store.create(candidates, "test", set_id=set_id)
        replay = await store.create(list(reversed(candidates)), "test", set_id=set_id)
        assert replay["sample_ids"] == original["sample_ids"]
        await store.append_samples(
            set_id, [candidate["revision_id"] for candidate in candidates], "test"
        )
        assert set((await store.get(set_id))["sample_ids"]) == {
            candidate["revision_id"] for candidate in candidates
        }
        first = candidates[0]
        with pytest.raises(AppError, match="检查"):
            await store.confirm(set_id, first["revision_id"], first["candidate_hash"], "test", {})
        await store.confirm(
            set_id, first["revision_id"], first["candidate_hash"], "test", {"text_checked": True}
        )
        with pytest.raises(AppError, match="逐项"):
            await store.complete(set_id, "test")
        await store.fail(set_id, candidates[1]["revision_id"], "TRANSLATION_MISMATCH", "test")
        async with sessions() as session:
            assert not await store.is_confirmed_within(
                session, first["revision_id"], first["candidate_hash"]
            )
        await short.command(
            "save",
            first["content_id"],
            "test",
            uuid4().hex,
            {
                "expected_version": 1,
                "revision_id": first["revision_id"],
                "expected_edit_version": 1,
                "content": content | {"title_en": "Changed"},
            },
        )
        with pytest.raises(AppError, match="变化"):
            await store.confirm(
                set_id,
                first["revision_id"],
                first["candidate_hash"],
                "test",
                {"text_checked": True},
            )
        with pytest.raises(AppError, match="变化"):
            await store.create(candidates, "test", set_id=set_id)
    finally:
        await engine.dispose()
