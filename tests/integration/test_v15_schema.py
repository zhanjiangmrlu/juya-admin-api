import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_v15_mysql_schema_has_unique_snapshot_and_nullable_capacity() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT version FROM schema_version WHERE id=1")) >= 17
            columns = (
                await conn.execute(
                    text(
                        "SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name='limited_campaign_version' "
                        "AND COLUMN_NAME='capacity'"
                    )
                )
            ).one()
            assert columns.IS_NULLABLE == "YES"
            keys = (
                (
                    await conn.execute(
                        text(
                            "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) columns_ "
                            "FROM information_schema.statistics WHERE table_schema=DATABASE() "
                            "AND table_name='favorite_snapshot' AND NON_UNIQUE=0 "
                            "GROUP BY INDEX_NAME"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "user_id,entry_type,entry_id,sense_id,source_id" in keys
            for name in (
                "short_reading",
                "short_reading_progress",
                "content_import_stage",
                "pronunciation_task",
                "sentence_audio_slice",
                "favorite_resource_reference",
            ):
                assert (
                    await conn.scalar(
                        text(
                            "SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema=DATABASE() AND TABLE_NAME=:name"
                        ),
                        {"name": name},
                    )
                    == 1
                )
    finally:
        await engine.dispose()
