import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks.celery_app import create_celery_app
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.modules.media.service import MediaAdminService


@pytest.mark.asyncio
async def test_real_redis_celery_batch_persists_once_without_ocr_provider() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    redis = os.getenv("JUYA_TEST_REDIS_URL")
    if not url or not redis:
        pytest.skip("isolated MySQL and Redis required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProductionStore(sessions)
    content = ContentService(SQLAlchemyContentRepository(sessions))
    admin = MediaAdminService(SQLAlchemyMediaAdminRepository(sessions))
    now = datetime.now(UTC)
    suffix = uuid4().hex
    series = await store.create_series("Celery fixture", "celery-" + suffix, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await content.create_revision(scene, None, "test", now)
    batch = await admin.create_batch(
        business_key="celery:" + suffix,
        job_type="TAGS",
        target_ids=(scene,),
        actor_id="test",
        now=now,
        input_payload={"tags": ["actual-worker"]},
    )
    queue = "v13-test-" + suffix
    environment = dict(
        os.environ,
        JUYA_DATABASE_URL=url,
        JUYA_REDIS_URL=redis,
        JUYA_ENVIRONMENT="test",
        JUYA_OCR_PROVIDER="disabled",
    )
    worker = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "juya_admin_api.infrastructure.tasks.celery_app:celery_app",
            "worker",
            "-P",
            "solo",
            "-c",
            "1",
            "-Q",
            queue,
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel",
            "ERROR",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    app = create_celery_app(Settings(environment="test", redis_url=redis))
    try:
        for _ in range(2):
            app.send_task(
                "juya.content.publish.batch_execute", kwargs={"batch_id": batch.id}, queue=queue
            )
        deadline = time.monotonic() + 30
        result = await admin.get_batch(batch.id)
        while (
            result.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
            and time.monotonic() < deadline
        ):
            assert worker.poll() is None, "isolated Celery worker exited"
            await asyncio.sleep(0.25)
            result = await admin.get_batch(batch.id)
        assert result.status == "COMPLETED", result.result_payload
        assert (await admin.list_batch_items(batch.id))[0].attempt_count == 1
        assert (await content.get_revision(draft.id)).content["tags"] == ["actual-worker"]
    finally:
        worker.terminate()
        await asyncio.to_thread(worker.wait, timeout=10)
        app.close()
        await engine.dispose()
