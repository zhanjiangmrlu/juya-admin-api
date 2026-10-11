"""Persistent switches and metadata ordering against isolated migrated MySQL."""

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore, decode
from juya_admin_api.modules.learning_features.store import CONFIG_KEY, LearningFeaturesStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.modules.v15_access.sql_adapters import SQLAuthorizer, SQLContentLoader
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_sql_recording_cas_replay_and_short_reading_metadata_ordering():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL after 0018 required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    now = datetime.now(UTC)
    actor, user = new_ulid(now), new_ulid(now)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            sessions = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            features = LearningFeaturesStore(sessions)
            readings = ShortReadingStore(sessions)
            production = ProductionStore(sessions)
            loader = SQLContentLoader(readings)
            try:
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("DELETE FROM system_config WHERE config_key=:key"), {"key": CONFIG_KEY}
                    )
                    await session.execute(
                        text(
                            "UPDATE learning_module_config SET enabled=1 WHERE "
                            "module_type='short_reading'"
                        )
                    )
                    await session.execute(
                        text(
                            "INSERT INTO user_account(public_id,juya_number,status) "
                            "VALUES(:id,:number,'ACTIVE')"
                        ),
                        {"id": user, "number": "T" + user[-19:]},
                    )
                assert await features.read() == {"recording_enabled": False, "version": 0}
                enabled = await features.command(
                    actor, "enable", {"expected_version": 0, "recording_enabled": True}
                )
                assert enabled == {"recording_enabled": True, "version": 1}
                with pytest.raises(AppError) as stale:
                    await features.command(
                        actor, "stale", {"expected_version": 0, "recording_enabled": False}
                    )
                assert stale.value.code == "CONFIG_VERSION_CONFLICT"
                disabled = await features.command(
                    actor, "disable", {"expected_version": 1, "recording_enabled": False}
                )
                assert disabled["version"] == 2
                assert (
                    await features.command(
                        actor, "enable", {"expected_version": 0, "recording_enabled": True}
                    )
                    == enabled
                )
                assert await features.read() == disabled
                with pytest.raises(AppError):
                    await features.command(
                        actor, "enable", {"expected_version": 0, "recording_enabled": False}
                    )
                series = await production.create_series("Order fixture", new_ulid(now), None)
                scene = await production.create_scene(
                    series["id"], "dialogue", actor=actor, key="scene"
                )
                async with sessions() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE scene_revision r JOIN scene s ON s.id=r.scene_id SET "
                            "r.status='PUBLISHED',s.status='PUBLISHED',s.published_revision"
                            "_id=r.id WHERE s.public_id=:id"
                        ),
                        {"id": scene},
                    )
                    detail = await loader(session, "dialogue", scene)
                    assert detail.recording_enabled is False
                await features.command(
                    actor, "enable-again", {"expected_version": 2, "recording_enabled": True}
                )
                async with sessions() as session:
                    assert (await loader(session, "dialogue", scene)).recording_enabled is True
                created = []
                for title in ("First", "Second"):
                    created.append(
                        await readings.command(
                            "create",
                            "new",
                            actor,
                            title,
                            {
                                "series_id": series["id"],
                                "content": {
                                    "title_en": title,
                                    "title_zh": "题",
                                    "safe_intro": "intro",
                                    "paragraphs_en": ["private body"],
                                    "translation_zh": "私有翻译",
                                },
                            },
                        )
                    )
                async with sessions() as session, session.begin():
                    for item in created:
                        await session.execute(
                            text(
                                "UPDATE short_reading SET "
                                "status='PUBLISHED',published_revision_id=:revision WHERE "
                                "public_id=:id"
                            ),
                            {"revision": item["revision_id"], "id": item["content_id"]},
                        )
                        await session.execute(
                            text(
                                "UPDATE short_reading_revision SET status='PUBLISHED' WHERE "
                                "public_id=:id"
                            ),
                            {"id": item["revision_id"]},
                        )
                        await session.execute(
                            text(
                                "INSERT INTO "
                                "short_reading_trial(short_reading_id,enabled,version,updated_b"
                                "y,updated_at) VALUES(:id,1,1,:actor,:now)"
                            ),
                            {"id": item["content_id"], "actor": actor, "now": now},
                        )
                    before = (
                        (
                            await session.execute(
                                text(
                                    "SELECT public_id,content_hash,content_snapshot FROM "
                                    "short_reading_revision WHERE public_id=:id"
                                ),
                                {"id": created[1]["revision_id"]},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    before_hash, before_text = (
                        before["content_hash"],
                        decode(before["content_snapshot"]),
                    )
                changed = await readings.command(
                    "sort-order",
                    created[1]["content_id"],
                    actor,
                    "move",
                    {"expected_version": 1, "sort_order": -9},
                )
                assert changed["sort_order"] == -9
                with pytest.raises(AppError):
                    await readings.command(
                        "sort-order",
                        created[1]["content_id"],
                        actor,
                        "old",
                        {"expected_version": 1, "sort_order": 7},
                    )
                assert (
                    await readings.command(
                        "sort-order",
                        created[1]["content_id"],
                        actor,
                        "move",
                        {"expected_version": 1, "sort_order": -9},
                    )
                    == changed
                )

                async def unused_signer(session: Any, request: Any, expires: Any) -> dict[str, Any]:
                    raise AssertionError("listing never signs a resource")

                access = V15AccessService(
                    sessions,
                    content_loader=loader,
                    authorizer=SQLAuthorizer(),
                    resource_signer=unused_signer,
                    credential_secret=b"isolated-recording-config-secret",
                )
                result = await access.short_reading_list(user, 1, 100, now)
                owned = [
                    item
                    for item in result["items"]
                    if item["content_id"] in {x["content_id"] for x in created}
                ]
                assert [item["content_id"] for item in owned] == [
                    created[1]["content_id"],
                    created[0]["content_id"],
                ]
                assert all(
                    "paragraphs_en" not in item
                    and "translation_zh" not in item
                    and "whole_audio" not in item
                    for item in owned
                )
                await readings.command(
                    "sort-order",
                    created[1]["content_id"],
                    actor,
                    "reset",
                    {"expected_version": 2, "sort_order": 0},
                )
                assert (
                    await readings.command(
                        "sort-order",
                        created[1]["content_id"],
                        actor,
                        "move",
                        {"expected_version": 1, "sort_order": -9},
                    )
                    == changed
                )
                tied = await access.short_reading_list(user, 1, 100, now)
                owned_ids = {item["content_id"] for item in created}
                assert [
                    item["content_id"] for item in tied["items"] if item["content_id"] in owned_ids
                ] == sorted(owned_ids)
                async with sessions() as session:
                    after = (
                        (
                            await session.execute(
                                text(
                                    "SELECT content_hash,content_snapshot FROM "
                                    "short_reading_revision WHERE public_id=:id"
                                ),
                                {"id": created[1]["revision_id"]},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert after["content_hash"] == before_hash
                    assert decode(after["content_snapshot"]) == before_text
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
