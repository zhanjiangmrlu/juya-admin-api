"""Real SQL checkpoints recover a pending Green task without another TTS or scan."""

import os
import wave
from dataclasses import asdict
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.content_security.aliyun import create_content_security_provider
from juya_admin_api.integrations.content_security.protocol import SecurityResult
from juya_admin_api.modules.pronunciation.domain import AudioResult, Config, Target, cache_key
from juya_admin_api.modules.pronunciation.models import attempts, budgets, cache, targets, tasks
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.modules.pronunciation.tasks import PronunciationWorker
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_provider", ["cloud-fixture", "local-fixture"])
async def test_sql_pending_review_survives_a_fresh_worker_and_keeps_one_paid_attempt(
    monkeypatch, recovery_provider
):
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlPronunciationRepository(sessions)
    target = Target(uuid4().hex, "hello" + uuid4().hex[:8])
    config = Config(enabled=True, price_version="test-tts-review")
    key = cache_key(target, config)
    period = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    object_key = "sealed/media/audio/recovery-" + uuid4().hex + ".wav"
    green_id = "green-" + uuid4().hex
    audio_buffer = BytesIO()
    with wave.open(audio_buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\0\0" * 16000)
    audio = audio_buffer.getvalue()
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value=AudioResult(audio, "tencent-kept", len(target.confirmed_text))
        )
    )
    oss = SimpleNamespace(freeze_bytes=AsyncMock(return_value=object_key))
    security = SimpleNamespace(
        scan_audio=AsyncMock(return_value=SecurityResult(green_id, "PENDING")),
        poll_audio=AsyncMock(
            side_effect=[SecurityResult(green_id, "PENDING"), SecurityResult(green_id, "PASSED")]
        ),
    )
    budget_owned = False
    try:
        async with sessions() as session, session.begin():
            assert (
                await session.scalar(select(budgets.c.period).where(budgets.c.period == period))
                is None
            )
            await session.execute(
                insert(budgets).values(
                    period=period, character_limit=100, cost_limit=3000, enabled=True
                )
            )
            budget_owned = True
            await session.execute(
                insert(targets).values(
                    **asdict(target), confirmed_by="tts-review-test", confirmed_at=0, created_at=0
                )
            )
        job = await repository.enqueue(target, key, config)
        await PronunciationWorker(repository, provider, oss, security).run(job["public_id"])
        pending = await repository.task(job["public_id"])
        assert pending["status"] == "RECOVER"
        assert pending["security_status"] == "PENDING"
        assert pending["security_request_id"] == green_id
        assert pending["request_id"] == "tencent-kept" and pending["audio"] == audio
        assert await repository.cached(key) is None
        with pytest.raises(AppError, match="租约"):
            await repository.fenced_update(
                pending | {"lease_token": "stale"}, security_request_id="incorrect-green-id"
            )
        assert (await repository.task(job["public_id"]))["security_request_id"] == green_id
        # Make the existing recovery due; do not create a replacement task or paid attempt.
        async with sessions() as session, session.begin():
            await session.execute(
                tasks.update().where(tasks.c.public_id == job["public_id"]).values(next_retry_at=0)
            )
        fresh_repository = SqlPronunciationRepository(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        if recovery_provider == "local-fixture":
            local_provider = create_content_security_provider(
                Settings(
                    environment="test",
                    content_security_enabled=True,
                    content_security_provider="local",
                    content_security_local_fixtures_only=True,
                ),
                oss,
            )
            await PronunciationWorker(fresh_repository, provider, oss, local_provider).run(
                job["public_id"]
            )
            retained = await fresh_repository.task(job["public_id"])
            assert retained["status"] == "RECOVER"
            assert retained["error_code"] == "MEDIA_SECURITY_UNAVAILABLE"
            assert (
                retained["security_status"] == "PENDING"
                and retained["security_request_id"] == green_id
            )
            assert retained["audio"] == audio and retained["request_id"] == "tencent-kept"
            assert await fresh_repository.cached(key) is None
            async with sessions() as session:
                ledger = (
                    (
                        await session.execute(
                            select(attempts).where(attempts.c.task_id == job["public_id"])
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(ledger) == 1 and ledger[0]["status"] == "SUCCEEDED"
                assert ledger[0]["provider_request_id"] == "tencent-kept"
            assert provider.generate.await_count == 1
            assert security.scan_audio.await_count == security.poll_audio.await_count == 1
            return

        async def unavailable_completion(*_args, **_kwargs):
            raise RuntimeError("local completion unavailable")

        monkeypatch.setattr(fresh_repository, "complete", unavailable_completion)
        await PronunciationWorker(fresh_repository, provider, oss, security).run(job["public_id"])
        passed_checkpoint = await fresh_repository.task(job["public_id"])
        assert passed_checkpoint["status"] == "RECOVER"
        assert passed_checkpoint["security_status"] == "PASSED"
        assert passed_checkpoint["security_request_id"] == green_id
        assert (
            passed_checkpoint["audio"] == audio
            and passed_checkpoint["request_id"] == "tencent-kept"
        )
        assert await fresh_repository.cached(key) is None
        async with sessions() as session, session.begin():
            await session.execute(
                tasks.update().where(tasks.c.public_id == job["public_id"]).values(next_retry_at=0)
            )
        fresh_repository = SqlPronunciationRepository(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        await PronunciationWorker(fresh_repository, provider, oss, security).run(job["public_id"])
        saved = await fresh_repository.task(job["public_id"])
        assert saved["status"] == "READY"
        assert saved["security_status"] == "PASSED" and saved["security_request_id"] == green_id
        assert saved["request_id"] == "tencent-kept" and saved["audio"] is None
        assert saved["error_code"] is None and saved["next_retry_at"] == 0
        assert (await fresh_repository.cached(key))["object_key"] == object_key
        async with sessions() as session:
            ledger = (
                (
                    await session.execute(
                        select(attempts).where(attempts.c.task_id == job["public_id"])
                    )
                )
                .mappings()
                .all()
            )
            assert len(ledger) == 1 and ledger[0]["status"] == "SUCCEEDED"
            assert ledger[0]["actual_characters"] == len(target.confirmed_text)
            assert ledger[0]["provider_request_id"] == "tencent-kept"
        assert provider.generate.await_count == 1
        assert security.scan_audio.await_count == 1
        assert [call.args[0] for call in security.poll_audio.await_args_list] == [
            green_id,
            green_id,
        ]
        assert oss.freeze_bytes.await_count == 1
    finally:
        async with sessions() as session, session.begin():
            task_id = await session.scalar(
                select(tasks.c.public_id).where(tasks.c.cache_key == key)
            )
            if task_id:
                await session.execute(attempts.delete().where(attempts.c.task_id == task_id))
            await session.execute(cache.delete().where(cache.c.cache_key == key))
            await session.execute(tasks.delete().where(tasks.c.cache_key == key))
            await session.execute(targets.delete().where(targets.c.public_id == target.public_id))
            from sqlalchemy import text

            await session.execute(
                text("DELETE FROM media_asset WHERE object_key=:key"), {"key": object_key}
            )
            if budget_owned:
                await session.execute(budgets.delete().where(budgets.c.period == period))
        await engine.dispose()
