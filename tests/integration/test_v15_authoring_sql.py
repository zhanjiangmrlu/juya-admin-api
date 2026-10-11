import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_modern_entry_confirmation_creates_immutable_controlled_target_and_cas():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    production = ProductionStore(sessions)
    authoring = SceneAuthoringStore(sessions, ProofreadingStore(sessions), StageStore(sessions))
    try:
        series = await production.create_series("Authoring", uuid4().hex, None)
        scene = await production.create_scene(series["id"], "dialogue")
        async with sessions() as session:
            revision = await session.scalar(
                text(
                    "SELECT r.public_id FROM scene_revision r "
                    "JOIN scene s ON s.draft_revision_id=r.id WHERE s.public_id=:id"
                ),
                {"id": scene},
            )
        saved = await production.save_revision(
            SceneRevision(
                revision,
                scene,
                None,
                content={
                    "schema_version": 2,
                    "title_en": "Hello",
                    "title_zh": "你好",
                    "dialogue": [],
                    "vocabulary": [
                        {
                            "english": "read",
                            "chinese": "阅读",
                            "pronunciation_text": "read",
                            "reading_variant": "present",
                            "allow_synthesis": True,
                            "confirmed": True,
                        }
                    ],
                },
            ),
            1,
        )
        snapshot = saved.content
        entry = snapshot["vocabulary"][0]
        assert not entry["confirmed"]
        with pytest.raises(AppError, match="人工"):
            await authoring.confirm_entry(revision, entry["entry_id"], saved.version, "test", False)
        result = await authoring.confirm_entry(
            revision, entry["entry_id"], saved.version, "test", True
        )
        confirmed = result["content"]["vocabulary"][0]
        assert confirmed["confirmed"]
        assert (entry["entry_id"], entry["sense_id"], entry["source_id"]) == (
            confirmed["entry_id"],
            confirmed["sense_id"],
            confirmed["source_id"],
        )
        async with sessions() as session:
            target = (
                (
                    await session.execute(
                        text("SELECT * FROM pronunciation_target WHERE public_id=:id"),
                        {"id": confirmed["pronunciation_target_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert target["confirmed_text"] == "read"
            assert target["reading_variant"] == "present"
            assert target["target_type"] == "WORD"
            assert target["confirmed_by"] == "test"
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT details,confirmed_by FROM content_entry_revision "
                            "WHERE revision_id=:revision AND entry_id=:entry"
                        ),
                        {"revision": revision, "entry": entry["entry_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert row["confirmed_by"] == "test"
            details = (
                json.loads(row["details"]) if isinstance(row["details"], str) else row["details"]
            )
            assert details["pronunciation_target_id"] == target["public_id"]
        with pytest.raises(AppError, match="变化"):
            await authoring.confirm_entry(revision, entry["entry_id"], saved.version, "test", True)
    finally:
        await engine.dispose()
