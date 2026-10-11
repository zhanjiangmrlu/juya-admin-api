import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.v15_backfill import FavoriteBackfill
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_source_multiplicity_idempotence_and_unknown_revision_preservation():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated JUYA_TEST_DATABASE_URL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine)
    now = datetime.now(UTC)
    public = new_ulid(now)
    user = None
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:id,:number,'ACTIVE')"
                ),
                {"id": public, "number": uuid4().hex[:10]},
            )
            user = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            await session.execute(
                text(
                    "INSERT INTO favorite_entry(public_id,user_id,entry_type,"
                    "normalized_key,entry_stable_id,favorited_at) VALUES(:id,:user,"
                    "'VOCABULARY','bank','entry-old',:now)"
                ),
                {"id": new_ulid(now), "user": user, "now": now},
            )
            favorite = await session.scalar(text("SELECT LAST_INSERT_ID()"))
            for locator in ["lost-location-a", "lost-location-b", "saved-resource"]:
                await session.execute(
                    text(
                        "INSERT INTO favorite_source(favorite_id,scene_id,"
                        "sentence_snapshot,source_locator,entry_snapshot) VALUES(:favorite,"
                        "'legacy-scene','saved original',:locator,:snapshot)"
                    ),
                    {
                        "favorite": favorite,
                        "locator": locator,
                        "snapshot": json.dumps(
                            {
                                "image_asset_id": "known-image",
                                "pronunciation": {
                                    "target_id": "known-target",
                                    "resource_version": "known-version",
                                },
                            }
                            if locator == "saved-resource"
                            else {}
                        ),
                    },
                )
        helper = FavoriteBackfill(sessions)
        report = await helper.run()
        assert not report["execute"]
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM favorite_snapshot WHERE user_id=:user"),
                    {"user": user},
                )
                == 0
            )
        # Concurrent scripts must not duplicate either source or its immutable revision.
        await asyncio.gather(helper.run(execute=True), helper.run(execute=True))
        await helper.run(execute=True)
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT r.*,f.source_id FROM favorite_snapshot f JOIN "
                            "favorite_snapshot_revision r ON r.favorite_id=f.id WHERE "
                            "f.user_id=:user"
                        ),
                        {"user": user},
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 3
            assert len({row["source_id"] for row in rows}) == 3
            assert all(
                row["revision_id"] is None and row["saved_content_version"] is None for row in rows
            )
            assert all(row["source_locator"] is None and row["legacy_incomplete"] for row in rows)
            assert all(
                row["source_sentence"] == "saved original" and row["meaning_zh"] == ""
                for row in rows
            )
            assert (
                sum(
                    row["image_asset_id"] is None and row["pronunciation_target_id"] is None
                    for row in rows
                )
                == 2
            )
            known = next(row for row in rows if row["image_asset_id"] == "known-image")
            assert known["pronunciation_target_id"] == "known-target"
            assert known["resource_version"] == "known-version"
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM favorite_source WHERE favorite_id=:id"),
                    {"id": favorite},
                )
                == 3
            )
    finally:
        if user:
            async with sessions() as session, session.begin():
                await session.execute(text("DELETE FROM user_account WHERE id=:id"), {"id": user})
        await engine.dispose()
