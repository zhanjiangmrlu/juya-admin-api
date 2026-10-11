import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
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
        scene = await production.create_scene(
            series["id"], "dialogue", actor="test", key=uuid4().hex
        )
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


@pytest.mark.asyncio
async def test_bulk_confirmation_binds_hash_and_edit_invalidates_completed_cohort():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    production = ProductionStore(sessions)
    proof = ProofreadingStore(sessions)
    authoring = SceneAuthoringStore(sessions, proof, StageStore(sessions))
    try:
        series = await production.create_series("Bulk authoring", uuid4().hex, None)
        candidates = []
        saved_revisions = []
        for _ in range(2):
            scene = await production.create_scene(
                series["id"], "word_card", actor="test", key=uuid4().hex
            )
            async with sessions() as session:
                revision_id = await session.scalar(
                    text(
                        "SELECT r.public_id FROM scene_revision r JOIN scene s "
                        "ON s.draft_revision_id=r.id WHERE s.public_id=:id"
                    ),
                    {"id": scene},
                )
            saved = await production.save_revision(
                SceneRevision(
                    revision_id,
                    scene,
                    None,
                    content={
                        "schema_version": 2,
                        "template_type": "word_card",
                        "title_en": "Lead",
                        "title_zh": "多义词",
                        "copyright": "Owned",
                        "source": "Isolated fixture",
                        "vocabulary": [
                            {"english": "lead", "chinese": "引导", "pronunciation_text": "lead"},
                            {"english": "lead", "chinese": "铅", "pronunciation_text": "lead"},
                        ],
                    },
                ),
                1,
            )
            saved_revisions.append(saved)
            candidates.append(
                {
                    "revision_id": revision_id,
                    "expected_version": saved.version,
                    "candidate_hash": snapshot_hash(saved.content),
                }
            )
        with pytest.raises(AppError):
            await authoring.confirm_entries_bulk(candidates, "test", False)
        failed = await authoring.confirm_entries_bulk(
            [candidates[0] | {"candidate_hash": "changed"}], "test", True
        )
        assert failed["items"][0]["error_code"] == "ENTRY_BATCH_HASH_CHANGED"
        result = await authoring.confirm_entries_bulk(candidates, "test", True)
        assert result["status"] == "CONFIRMED"
        for item in result["items"]:
            entries = item["content"]["vocabulary"]
            assert all(entry["confirmed"] for entry in entries)
            assert len({entry["entry_id"] for entry in entries}) == 2
            assert len({entry["sense_id"] for entry in entries}) == 2
            assert len({entry["pronunciation_target_id"] for entry in entries}) == 2
        cohort = await proof.create(
            [
                {
                    "content_type": "word_card",
                    "content_id": saved.scene_id,
                    "revision_id": saved.id,
                    "candidate_hash": snapshot_hash(item["content"]),
                }
                for saved, item in zip(saved_revisions, result["items"], strict=True)
            ],
            "test",
        )
        assert cohort["normal_count"] == 2
        await proof.append_samples(
            cohort["public_id"], [saved.id for saved in saved_revisions], "test"
        )
        for candidate in cohort["candidates"]:
            await proof.confirm(
                cohort["public_id"],
                candidate["revision_id"],
                candidate["candidate_hash"],
                "test",
                {"text_checked": True},
            )
        await proof.complete(cohort["public_id"], "test")
        first = saved_revisions[0]
        changed = result["items"][0]["content"] | {"source": "Edited source"}
        await production.save_revision(
            SceneRevision(first.id, first.scene_id, None, content=changed),
            result["items"][0]["edit_version"],
        )
        assert (await proof.get(cohort["public_id"]))["status"] == "PENDING"
        async with sessions() as session:
            second = cohort["candidates"][1]
            assert not await proof.is_confirmed_within(
                session, second["revision_id"], second["candidate_hash"]
            )
    finally:
        await engine.dispose()
