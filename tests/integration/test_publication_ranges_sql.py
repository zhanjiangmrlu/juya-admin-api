"""Real MySQL ranges/stages, fake publisher callbacks: state-machine acceptance only."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.publication_ranges import PublicationRangeService
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_range_subset_partial_retry_recovery_and_stale_confirmation():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    calls = []
    fail = True
    prefix = uuid4().hex
    candidates = [
        dict(
            content_type="dialogue",
            content_id=prefix + str(i),
            revision_id=prefix + str(i),
            expected_version=1,
            candidate_hash="a" * 64,
            warning_codes=[],
        )
        for i in range(3)
    ]

    async def inspect(candidate):
        return {**candidate, "ready": True}

    async def publish(candidate, actor, key):
        calls.append((candidate["revision_id"], key))
        if fail and candidate["revision_id"] == candidates[1]["revision_id"]:
            raise AppError("INJECTED_ITEM_FAILURE", "测试注入失败", 409)
        return {"published_revision_id": candidate["revision_id"]}

    service = PublicationRangeService(sessions, StageStore(sessions), inspect, publish)
    try:
        preview = await service.preview(candidates, "test")
        token = preview["token"]
        selected = [candidate["revision_id"] for candidate in candidates[:2]]
        fields = dict(
            manual_confirmed=True,
            expected_version=preview["version"],
            collection_hash=preview["collection_hash"],
            selected_revision_ids=selected,
        )
        with pytest.raises(AppError) as selection:
            await service.confirm(
                token, "test", "bad-subset", **{**fields, "selected_revision_ids": ["foreign"]}
            )
        assert selection.value.code == "PUBLICATION_SELECTION_INVALID"
        with pytest.raises(AppError):
            await service.confirm(
                token, "test", "not-confirmed", **{**fields, "manual_confirmed": False}
            )
        receipt = await service.confirm(token, "test", "first", **fields)
        assert await service.confirm(token, "test", "first", **fields) == receipt
        first = await service.run(receipt["stage_id"], "worker")
        assert first["status"] == "PARTIAL"
        assert len(calls) == 2
        assert candidates[2]["revision_id"] not in first["results"]["items"]
        with pytest.raises(AppError):
            await service.confirm(token, "test", "stale", **fields)
        fail = False
        retry = await service.confirm(
            token, "test", "retry", **{**fields, "expected_version": first["version"]}
        )
        done = await service.run(retry["stage_id"], "worker-two")
        assert done["status"] == "SUCCEEDED"
        assert len(calls) == 3
        assert calls[1][1] == calls[2][1]
        # New state/TTL cannot invalidate a committed confirmation receipt.
        assert (
            await service.confirm(
                token, "test", "first", **fields, now=datetime.now(UTC) + timedelta(hours=1)
            )
            == receipt
        )
        expired = await service.preview(
            [candidates[2]], "test", now=datetime.now(UTC) - timedelta(minutes=16)
        )
        with pytest.raises(AppError) as expiration:
            await service.confirm(
                expired["token"],
                "test",
                "expired",
                manual_confirmed=True,
                expected_version=1,
                collection_hash=expired["collection_hash"],
                selected_revision_ids=[candidates[2]["revision_id"]],
            )
        assert expiration.value.code == "PUBLICATION_PREVIEW_EXPIRED"
        with pytest.raises(AppError):
            await service.recover(token, "other-actor")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_range_cancelled_stage_never_calls_publisher():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    called = []
    candidate = dict(
        content_type="dialogue",
        content_id=uuid4().hex,
        revision_id=uuid4().hex,
        expected_version=1,
        candidate_hash="a" * 64,
    )

    async def inspect(value):
        return {**value, "ready": True}

    async def publish(value, actor, key):
        called.append(key)
        return {}

    service = PublicationRangeService(sessions, StageStore(sessions), inspect, publish)
    try:
        preview = await service.preview([candidate], "test")
        receipt = await service.confirm(
            preview["token"],
            "test",
            uuid4().hex,
            manual_confirmed=True,
            expected_version=1,
            collection_hash=preview["collection_hash"],
            selected_revision_ids=[candidate["revision_id"]],
        )
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE content_import_stage SET status='CANCELLED',fencing_versio"
                    "n=fencing_version+1 WHERE public_id=:id"
                ),
                {"id": receipt["stage_id"]},
            )
        assert await service.run(receipt["stage_id"], "worker") == {
            "stage_id": receipt["stage_id"],
            "claimed": False,
        }
        assert called == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_short_reading_one_thousand_normal_candidates_freeze_twenty_samples():
    from juya_admin_api.modules.content.production_store import ProductionStore
    from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
    from juya_admin_api.modules.short_reading.store import ShortReadingStore

    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    proof = ProofreadingStore(sessions)
    short = ShortReadingStore(sessions)
    try:
        series = await ProductionStore(sessions).create_series(
            "1000 normal publication fixture", uuid4().hex, None
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
            copyright_source="Owned test fixture",
        )
        candidates = []
        async with sessions() as session, session.begin():
            for _ in range(1000):
                created = await short.create_within(session, series["id"], content, "test")
                candidate_hash = await session.scalar(
                    text("SELECT content_hash FROM short_reading_revision WHERE public_id=:id"),
                    {"id": created["revision_id"]},
                )
                candidates.append(
                    dict(
                        content_type="short_reading",
                        content_id=created["content_id"],
                        revision_id=created["revision_id"],
                        candidate_hash=candidate_hash,
                    )
                )
        empty = await proof.create([], "test")
        assert empty["normal_count"] == 0 and empty["sample_ids"] == []
        cohort = await proof.create(candidates, "test")
        assert cohort["normal_count"] == 1000
        assert len(cohort["sample_ids"]) == 20
        assert len(set(cohort["sample_ids"])) == 20
        assert set(cohort["sample_ids"]) <= {candidate["revision_id"] for candidate in candidates}
        replay = await proof.create(candidates, "test", set_id=cohort["public_id"])
        assert replay["sample_ids"] == cohort["sample_ids"]
        assert replay["collection_hash"] == cohort["collection_hash"]
        # This is real SQL validation and cohort freezing, not cloud/audio/human approval.
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_worker_cannot_save_progress_and_recovery_reuses_publish_key():
    import asyncio

    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    receipts = {}
    calls = []
    candidate = dict(
        content_type="dialogue",
        content_id=uuid4().hex,
        revision_id=uuid4().hex,
        expected_version=1,
        candidate_hash="a" * 64,
    )

    async def inspect(value):
        return {**value, "ready": True}

    async def publish(value, actor, key):
        calls.append(key)
        if key not in receipts:
            receipts[key] = {"revision_id": value["revision_id"]}
            await asyncio.sleep(1.1)
        return receipts[key]

    service = PublicationRangeService(sessions, StageStore(sessions), inspect, publish)
    try:
        preview = await service.preview([candidate], "test")
        receipt = await service.confirm(
            preview["token"],
            "test",
            uuid4().hex,
            manual_confirmed=True,
            expected_version=1,
            collection_hash=preview["collection_hash"],
            selected_revision_ids=[candidate["revision_id"]],
        )
        with pytest.raises(AppError) as stale:
            await service.run(receipt["stage_id"], "old-worker", lease_seconds=1)
        assert stale.value.code == "IMPORT_STAGE_LEASE_LOST"
        recovered = await service.recover(preview["token"], "test")
        assert recovered["results"]["items"] == {}
        done = await service.run(receipt["stage_id"], "new-worker", lease_seconds=30)
        assert done["status"] == "SUCCEEDED"
        assert len(receipts) == 1 and calls[0] == calls[1]
    finally:
        await engine.dispose()
