"""Large UTF-8 JSON packets remain complete across real asyncmy/MySQL recovery paths."""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.infrastructure.db.bounded_json import CHUNK_CHARS
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.publication_ranges import PublicationRangeService
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore


@pytest.mark.asyncio
async def test_multi_megabyte_unicode_proof_import_range_recovery_preserves_complete_json(tmp_path):
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = uuid4().hex
    actor = uuid4().hex
    values = [{"revision_id": f"{identity}-{i}", "snapshot": "学习😀𠀀" * 200} for i in range(1000)]
    encoded = json.dumps(values, ensure_ascii=False)
    assert len(encoded.encode()) > 2_000_000
    queries = []

    def record(connection, cursor, statement, parameters, context, many):
        if "SUBSTRING(CAST" in statement:
            queries.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)

    async def unused(*args):
        raise AssertionError("Transport recovery test must not publish or bind media")

    production = ProductionStore(sessions)
    short = ShortReadingStore(sessions)
    imports = ImportService(sessions, tmp_path, production, short, unused)
    ranges = PublicationRangeService(sessions, StageStore(sessions), unused, unused)
    proof = ProofreadingStore(sessions)
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO proofreading_set(public_id,collection_hash,candidates"
                    ",normal_count,sample_ids,status,created_by) VALUES(:id,:hash,:jso"
                    "n,1000,'[]','PENDING',:actor)"
                ),
                {"id": identity, "hash": "a" * 64, "json": encoded, "actor": actor},
            )
            await session.execute(
                text(
                    "INSERT INTO publication_range(public_id,actor_id,collection_hash,"
                    "candidates,results,created_at,expires_at) VALUES(:id,:actor,:hash"
                    ",:json,:results,UTC_TIMESTAMP(6),DATE_ADD(UTC_TIMESTAMP(6),INTERV"
                    "AL 15 MINUTE))"
                ),
                {
                    "id": identity,
                    "actor": actor,
                    "hash": "a" * 64,
                    "json": encoded,
                    "results": json.dumps({"items": values}, ensure_ascii=False),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO content_import_batch(public_id,namespace,series_id,co"
                    "ntent_type,import_mode,asset_id,input_hash,manifest,status,create"
                    "d_by) VALUES(:id,'test',:series,'dialogue','ZIP',:asset,:hash,:js"
                    "on,'PREVIEW_READY',:actor)"
                ),
                {
                    "id": identity,
                    "series": uuid4().hex,
                    "asset": uuid4().hex,
                    "hash": "a" * 64,
                    "json": json.dumps({"items": values}, ensure_ascii=False),
                    "actor": actor,
                },
            )
        async with sessions() as session, session.begin():
            await production.remember_creation(
                session,
                "bounded-json-test",
                actor,
                identity,
                "a" * 64,
                {"items": values},
                datetime.now(UTC),
            )
            assert await production.creation_replay(
                session, "bounded-json-test", actor, identity, "a" * 64
            ) == {"items": values}
        cohort = await proof.get(identity)
        assert cohort["candidates"] == values and cohort["normal_count"] == 1000
        async with sessions() as session, session.begin():
            locked = await proof.lock_set(session, identity)
            assert locked["candidates"] == values
            batch = await imports.batch(session, identity, True)
            assert batch["manifest"] == {"items": values}
        recovered = await ranges.recover(identity, actor)
        assert recovered["candidates"] == values
        assert recovered["results"] == {"items": values}
        assert len(queries) > 100
        assert all("FOR SHARE" in query for query in queries)
        async with sessions() as session:
            json_size = await session.scalar(
                text(
                    "SELECT CHAR_LENGTH(CAST(candidates AS CHAR CHARACTER SET utf8mb4)"
                    ") FROM proofreading_set WHERE public_id=:id"
                ),
                {"id": identity},
            )
            piece = await session.scalar(
                text(
                    "SELECT SUBSTRING(CAST(candidates AS CHAR CHARACTER SET utf8mb4),1"
                    ",:length) FROM proofreading_set WHERE public_id=:id"
                ),
                {"id": identity, "length": CHUNK_CHARS},
            )
        assert json_size > CHUNK_CHARS and len(piece.encode()) <= 4 * CHUNK_CHARS
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM idempotency_record WHERE actor_id=:actor"), {"actor": actor}
            )
            for table in ("proofreading_set", "publication_range", "content_import_batch"):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE public_id=:id"), {"id": identity}
                )
        await engine.dispose()
