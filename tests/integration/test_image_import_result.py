import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore


@pytest.mark.asyncio
async def test_import_distinguishes_creation_reuse_and_preserves_replay_result() -> None:
    # 功能:验证首次导入、重复图片复用及响应丢失重试的结果提示一致
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProductionStore(sessions, require_review=False)
    try:
        series = await store.create_series("Import result", "import-" + uuid4().hex, None)
        asset_id = uuid4().hex[:26]
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,asset_type,content_type,"
                    "size_bytes,sha256,status,security_status,created_by,created_at,width,height) "
                    "VALUES (:id,:key,'images','image/png',100,:sha,'CONFIRMED','SKIPPED',"
                    "'test',:now,100,100)"
                ),
                {
                    "id": asset_id,
                    "key": "fixtures/" + asset_id,
                    "sha": uuid4().hex * 2,
                    "now": datetime.now(UTC),
                },
            )
        first = await store.import_images(series["id"], "dialogue", [asset_id], "test", "first")
        assert first["reused_scene_ids"] == []
        assert len(first["scene_ids"]) == 1
        async with sessions() as session:
            before = await session.scalar(
                text("SELECT updated_at FROM scene WHERE public_id=:id"),
                {"id": first["scene_ids"][0]},
            )
        duplicate = await store.import_images(
            series["id"], "dialogue", [asset_id], "test", "duplicate"
        )
        assert duplicate["scene_ids"] == first["scene_ids"]
        assert duplicate["reused_scene_ids"] == first["scene_ids"]
        assert (
            await store.import_images(series["id"], "dialogue", [asset_id], "test", "first")
            == first
        )
        assert (
            await store.import_images(series["id"], "dialogue", [asset_id], "test", "duplicate")
            == duplicate
        )
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT updated_at FROM scene WHERE public_id=:id"),
                    {"id": first["scene_ids"][0]},
                )
                == before
            )
    finally:
        await engine.dispose()
