import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.audio_pipeline.repository import SQLAudioRepository
from juya_admin_api.modules.audio_pipeline.service import SliceInput
from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, SliceResult
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_mysql_slice_fencing_idempotency_and_boundary_revision():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    revision = new_ulid(now)
    current = SliceInput(
        revision,
        new_ulid(now),
        "Hello",
        "sealed/media/source.wav",
        new_ulid(now),
        "a" * 64,
        Boundary(0, 1000),
        "admin",
        content_id=new_ulid(now),
    )

    async def read(revision_id, sentence_id):
        return current

    async def locked(session, revision_id, sentence_id):
        # Simulate the canonical content row lock using a named test row.
        await session.execute(text("SELECT id FROM schema_version WHERE id=1 FOR UPDATE"))
        return current

    repository = SQLAudioRepository(sessions, read, locked, FFmpegSlicer.encoding_config)
    data = revision.encode()
    result = SliceResult(data, 1000, hashlib.sha256(data).hexdigest())
    try:
        saved = await asyncio.gather(
            *[
                repository.commit_if_current(current, result, "sealed/media/" + revision + ".wav")
                for _ in range(2)
            ]
        )
        assert saved[0] == saved[1]
        assert saved[0] is not None
        old_input = current
        current = replace(current, boundary=Boundary(0, 900))
        assert await repository.find_ready(current) is None
        assert await repository.commit_if_current(old_input, result, "sealed/media/old.wav") is None
        with pytest.raises(ValueError, match="dependencies changed"):
            await repository.confirm(
                revision, current.sentence_id, saved[0].dependency_hash, "admin"
            )
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM sentence_audio_slice WHERE revision_id=:id"),
                    {"id": revision},
                )
                == 1
            )
    finally:
        await engine.dispose()
