import io
import os
import shutil
import time
import wave
from dataclasses import asdict
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.pronunciation.admin_store import PronunciationAdminStore
from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.pronunciation.models import attempts, cache, targets, tasks
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_admin_actual_cache_facts_safe_status_and_retained_recovery():
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    target = Target(uuid4().hex, "hello " + uuid4().hex)
    config = Config()
    key = cache_key(target, config)
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 6400)
    payload = data.getvalue()
    ffprobe = shutil.which("ffprobe")
    assert ffprobe, "actual ffprobe inspection required"
    inspected = await inspect_media(payload, "audio", ffprobe)
    object_key, sha = "sealed/media/audio/" + uuid4().hex + ".wav", inspected.sha256
    repository = SqlPronunciationRepository(sessions)
    verify, sign = AsyncMock(), AsyncMock(return_value="signed-fixed")
    store = PronunciationAdminStore(sessions, config, verify_asset=verify, sign_asset=sign)
    now = int(time.time())
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                insert(targets).values(
                    **asdict(target), confirmed_by="admin", confirmed_at=now, created_at=now
                )
            )
        job = await repository.enqueue(target, key, config)
        claim = await repository.claim(job["public_id"], now)
        await repository.fenced_update(claim, audio=payload)
        await repository.complete(
            claim,
            object_key,
            inspected.duration_ms,
            sha256=sha,
            size_bytes=inspected.size,
            content_type=inspected.content_type,
            security_status="PASSED",
            security_request_id="private-provider-request",
        )
        result = await store.get(target.public_id)
        assert result["source"] == "CACHE" and result["task"]["status"] == "READY"
        assert "object_key" not in str(result) and "private-provider-request" not in str(result)
        fixed = await store.preview(target.public_id, job["public_id"])
        assert fixed["sha256"] == sha and fixed["duration_ms"] == inspected.duration_ms
        assert fixed["expires_in"] == 60
        verify.assert_awaited_once()
        sign.assert_awaited_once_with(verify.call_args.args[0], 60)
        async with sessions() as session, session.begin():
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == job["public_id"])
                .values(status="UNKNOWN", audio=b"paid-bytes")
            )
        with pytest.raises(AppError) as exc:
            await store.retry(job["public_id"])
        assert exc.value.code == "PRONUNCIATION_RETRY_UNSAFE"
        async with sessions() as session, session.begin():
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == job["public_id"])
                .values(status="RECOVER", outbox_pending=False)
            )
        await store.retry(job["public_id"])
        async with sessions() as session:
            assert await session.scalar(
                text("SELECT outbox_pending FROM pronunciation_task WHERE public_id=:id"),
                {"id": job["public_id"]},
            )
            assert (
                await session.scalar(
                    text("SELECT COUNT(*) FROM cloud_usage_attempt WHERE task_id=:id"),
                    {"id": job["public_id"]},
                )
                == 0
            )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(attempts.delete().where(attempts.c.task_id == job["public_id"]))
            await session.execute(cache.delete().where(cache.c.cache_key == key))
            await session.execute(tasks.delete().where(tasks.c.cache_key == key))
            await session.execute(targets.delete().where(targets.c.public_id == target.public_id))
            await session.execute(
                text("DELETE FROM media_asset WHERE object_key=:key"), {"key": object_key}
            )
        await engine.dispose()
