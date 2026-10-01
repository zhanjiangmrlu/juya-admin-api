import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.batch_executor import BatchExecutor, ContentBatchOperations
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.modules.media.service import MediaAdminService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_mysql_batch_operations_use_same_draft_and_pin_publish_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProductionStore(sessions)
    content = ContentService(SQLAlchemyContentRepository(sessions))
    repository = SQLAlchemyMediaAdminRepository(sessions)
    admin = MediaAdminService(repository)
    now = datetime.now(UTC)
    series = await store.create_series("Batch fixture", "batch-" + uuid4().hex, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await content.create_revision(scene, None, "test", now)
    image, audio, target, version, package = [new_ulid(now) for _ in range(5)]
    try:
        async with sessions() as session, session.begin():
            for asset, kind, duration in [(image, "images", None), (audio, "audio", 3000)]:
                await session.execute(
                    text(
                        "INSERT INTO media_asset(public_id,object_key,asset_type,"
                        "content_type,size_bytes,"
                        "sha256,status,security_status,created_by,created_at,width,height,"
                        "duration_ms) "
                        "VALUES(:id,:key,:kind,:mime,100,:sha,'CONFIRMED','PASSED','test',"
                        ":now,100,100,:dur)"
                    ),
                    {
                        "id": asset,
                        "key": f"uploads/{kind}/test/fixtures/{asset}",
                        "kind": kind,
                        "mime": "image/png" if kind == "images" else "audio/wav",
                        "sha": uuid4().hex * 2,
                        "now": now,
                        "dur": duration,
                    },
                )
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,"
                    "target_type) VALUES(:id,:key,'scene')"
                ),
                {"id": target, "key": f"scene:{scene}"},
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,"
                    "source,status,created_at) "
                    "SELECT :id,t.id,m.id,1,'MANUAL','ACTIVE',:now FROM audio_target t,"
                    "media_asset m "
                    "WHERE t.public_id=:target AND m.public_id=:asset"
                ),
                {"id": version, "target": target, "asset": audio, "now": now},
            )
            await session.execute(
                text(
                    "INSERT INTO content_package(public_id,name,status) VALUES(:id,"
                    "'Batch fixture','ACTIVE')"
                ),
                {"id": package},
            )
        saved = await content.save_revision(
            draft.id,
            {
                "title_en": "Batch",
                "title_zh": "批量",
                "original_image_asset_id": image,
                "copyright": "Fixture permission",
                "source": "Fixture source",
                "audio": {
                    "target_id": target,
                    "version_id": version,
                    "asset_id": audio,
                    "duration_ms": 3000,
                },
                "dialogue": [
                    {
                        "id": uuid4().hex,
                        "speaker": "A",
                        "english": "Hello, good morning.",
                        "chinese": "你好",
                        "start_ms": 0,
                        "end_ms": 2800,
                        "audio_version_id": version,
                        "timing_confirmed": True,
                    }
                ],
                "vocabulary": [{"english": "Hello", "chinese": "你好"}],
                "chunks": [{"english": "good morning", "chinese": "早上好"}],
            },
            expected_version=1,
            actor_id="test",
        )
        operations = ContentBatchOperations(content, store)
        worker = BatchExecutor(admin, repository, operations)

        async def run(kind: str, payload: dict[str, object]) -> str:
            batch = await admin.create_batch(
                business_key=f"test:{kind}:{uuid4().hex}",
                job_type=kind,
                target_ids=(scene,),
                actor_id="test",
                now=now,
                input_payload=payload,
            )
            await asyncio.gather(worker.run(batch.id, now), worker.run(batch.id, now))
            result = await admin.get_batch(batch.id)
            assert result.status == "COMPLETED", result.result_payload
            assert (await admin.list_batch_items(batch.id))[0].attempt_count == 1
            return result.id

        tagged = await admin.create_batch(
            business_key=f"tags:{uuid4().hex}",
            job_type="TAGS",
            target_ids=(scene, "missing-scene"),
            actor_id="test",
            now=now,
            input_payload={"tags": ["travel"]},
        )
        await worker.run(tagged.id, now)
        result = await admin.get_batch(tagged.id)
        assert (result.success_count, result.failure_count) == (1, 1)
        assert (await content.get_revision(saved.id)).content["tags"] == ["travel"]
        await run("COPYRIGHT", {"copyright": "Updated fixture permission"})
        await run("PACKAGE", {"package_id": package})
        await run("VALIDATE", {})
        exported = await run("EXPORT", {})
        exported_payload = (await admin.get_batch(exported)).result_payload
        assert "signature=" not in str(exported_payload) and "https://" not in str(exported_payload)
        current = await content.get_revision(saved.id)
        await run("PUBLISH", {"expected_versions": {scene: current.version}})
        assert (await store.full_scene(scene))["content"]["tags"] == ["travel"]
        await run("OFFLINE", {})
        assert (await content.get_scene(scene)).status == "OFFLINE"
        original_validate = ContentService.validate_publish

        async def invalidate_after_check(*args: object, **kwargs: object) -> object:
            result = await original_validate(*args, **kwargs)
            async with sessions() as session, session.begin():
                await session.execute(
                    text("UPDATE media_asset SET security_status='FAILED' WHERE public_id=:id"),
                    {"id": image},
                )
            return result

        with monkeypatch.context() as patch:
            patch.setattr(ContentService, "validate_publish", invalidate_after_check)
            with pytest.raises(AppError) as blocked:
                await operations("RESTORE", scene, {}, "test", "restore-race", now)
            assert blocked.value.code == "PUBLISH_CHECK_FAILED"
        assert (await content.get_scene(scene)).status == "OFFLINE"
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE media_asset SET security_status='PASSED' WHERE public_id=:id"),
                {"id": image},
            )
        await run("RESTORE", {})
        assert (await content.get_scene(scene)).status == "PUBLISHED"
    finally:
        await engine.dispose()
