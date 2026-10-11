"""Isolated MySQL acceptance: independent lifecycle, replay, CAS and immutability."""

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_short_reading_publish_offline_and_replay() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def proofread(session, revision_id, content_hash):
        return True

    store = ShortReadingStore(sessions, check_confirmation=proofread)
    try:
        series = await ProductionStore(sessions).create_series("Short readings", uuid4().hex, None)
        content = dict(
            title_en="Read",
            title_zh="阅读",
            category="Daily",
            topic="Life",
            level="A1",
            safe_intro="A day",
            paragraphs_en=["A full English paragraph."],
            translation_zh="完整中文译文",
            copyright_status="CONFIRMED",
            copyright_source="Owned fixture",
        )
        created = await store.command(
            "create", "new", "test", uuid4().hex, {"series_id": series["id"], "content": content}
        )
        identity, revision = created["content_id"], created["revision_id"]
        payload = {"expected_version": 1, "revision_id": revision, "manual_confirmed": True}
        key = uuid4().hex
        published = await store.command("publish", identity, "test", key, payload)
        assert published["version"] == 2
        async with sessions() as session:
            detail = await store.published_detail_within(session, identity)
            assert detail.paragraphs_en == content["paragraphs_en"]
            assert detail.whole_audio is None
            assert not hasattr(detail, "words")
        await store.command("offline", identity, "test", uuid4().hex, {"expected_version": 2})
        assert await store.command("publish", identity, "test", key, payload) == published
        with pytest.raises(AppError, match="短文版本已变化"):
            await store.command(
                "trial", identity, "test", uuid4().hex, {"expected_version": 1, "enabled": True}
            )
        with pytest.raises(AppError, match="已发布版本不可修改"):
            await store.command(
                "save",
                identity,
                "test",
                uuid4().hex,
                {
                    "expected_version": 3,
                    "revision_id": revision,
                    "expected_edit_version": 1,
                    "content": content,
                },
            )
    finally:
        await engine.dispose()
