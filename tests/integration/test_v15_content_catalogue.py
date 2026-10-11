"""Verify mixed-template paging on the isolated MySQL schema."""

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.catalogue_router import CataloguePage, read_catalogue
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_contracts import IMPORT_NAMESPACE
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.short_reading.store import ShortReadingStore


@pytest.mark.asyncio
async def test_mysql_catalogue_filters_before_global_paging() -> None:
    """Create three isolated drafts and verify real SQL counts and bilingual projections."""
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    production = ProductionStore(sessions)
    try:
        series = await production.create_series("Catalogue fixture", uuid4().hex, None)
        identities = []
        for template, en, zh in [("dialogue", "Cafe", "咖啡对话"), ("vocabulary", "Words", "词卡")]:
            identity = await production.create_scene(
                series["id"], template, actor="test", key=uuid4().hex
            )
            identities.append(identity)
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE scene_revision r JOIN scene s ON s.draft_revision_id=r.id "
                        "SET r.content_snapshot=:snapshot WHERE s.public_id=:id"
                    ),
                    {"id": identity, "snapshot": json.dumps({"title_en": en, "title_zh": zh})},
                )
        reading = await ShortReadingStore(sessions).command(
            "create",
            "new",
            "test",
            uuid4().hex,
            {
                "series_id": series["id"],
                "content": {
                    "title_en": "Garden",
                    "title_zh": "城市花园",
                    "category": "Daily",
                    "topic": "Life",
                    "level": "A1",
                    "safe_intro": "A garden",
                    "paragraphs_en": ["A garden in the city."],
                    "translation_zh": "城市中的花园。",
                    "copyright_status": "CONFIRMED",
                    "copyright_source": "Owned fixture",
                },
            },
        )
        async with sessions() as session:
            first = await read_catalogue(session, series_id=series["id"], page_size=2)
            second = await read_catalogue(session, series_id=series["id"], page_size=2, page=2)
            CataloguePage.model_validate(first)
            assert first["total"] == second["total"] == 3
            rows = first["items"] + second["items"]
            assert {row["content_type"] for row in rows} == {
                "dialogue",
                "word_card",
                "short_reading",
            }
            assert {row["id"] for row in rows} == {*identities, reading["content_id"]}
            found = await read_catalogue(session, series_id=series["id"], query="城市")
            assert found["total"] == 1 and found["items"][0]["title_en"] == "Garden"
            cards = await read_catalogue(session, series_id=series["id"], content_type="word_card")
            assert cards["total"] == 1 and cards["items"][0]["title_zh"] == "词卡"
            assert (await read_catalogue(session, series_id=series["id"], status="PUBLISHED"))[
                "total"
            ] == 0
            assert (await read_catalogue(session, series_id=series["id"], query="%"))["total"] == 0

        async def reviewed(session, revision_id, content_hash):
            return True

        published = await ShortReadingStore(sessions, check_confirmation=reviewed).command(
            "publish",
            reading["content_id"],
            "test",
            uuid4().hex,
            {
                "expected_version": 1,
                "revision_id": reading["revision_id"],
                "manual_confirmed": True,
            },
        )
        external = uuid4().hex
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO import_content_mapping(namespace,content_type,import_content_id,"
                    "content_id,input_hash) VALUES(:ns,'short_reading',:external,:id,:hash)"
                ),
                {
                    "ns": IMPORT_NAMESPACE,
                    "external": external,
                    "id": reading["content_id"],
                    "hash": "a" * 64,
                },
            )
        reader = ImportService.__new__(ImportService)
        async with sessions() as session:
            mapping = await reader.current_mapping(session, "short_reading", external)
            assert mapping is not None
            assert mapping["draft_revision_id"] is None
            assert mapping["published_revision_id"] == reading["revision_id"]
            assert mapping["current_version"] == published["version"] == 2
    finally:
        await engine.dispose()
