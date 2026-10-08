import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.domain import PreviewConfig
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.schemas import SceneEntry
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
@pytest.mark.parametrize("require_review,security_status", [(True, "PASSED"), (False, "SKIPPED")])
async def test_published_snapshot_pins_lexicon_audio_and_resources_with_live_checks(
    require_review: bool, security_status: str
) -> None:
    # 功能:验证发布快照固定词库、音频和资源引用并检查实时状态。
    # 参数:
    #     require_review: 是否要求云端安全审核;关闭时仍检查资源文件事实。
    #     security_status: 媒体审核状态,如 SAFE、SKIPPED 或 BLOCKED。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = ProductionStore(sessions, require_review=require_review)
    service = ContentService(SQLAlchemyContentRepository(sessions, require_review=require_review))
    now = datetime.now(UTC)
    slug = "v13-" + uuid4().hex[:10]
    series = await store.create_series("SQL version pinning", slug, None)
    scene = await store.create_scene(series["id"], "dialogue")
    draft = await service.create_revision(scene, None, "test", now)
    image, audio, target, version, cover = [new_ulid(now) for _ in range(5)]
    async with sessions() as session, session.begin():
        for asset_id, kind, duration in [
            (image, "images", None),
            (audio, "audio", 3000),
            (cover, "images", None),
        ]:
            await session.execute(
                text(
                    "INSERT INTO "
                    "media_asset(public_id,object_key,asset_type,content_type,size_bytes,sha256,"
                    "status,security_status,created_by,created_at,width,height,duration_ms) "
                    "VALUES "
                    "(:id,:key,:kind,:mime,100,:sha,'CONFIRMED',:security,'test',:now,100,"
                    "100,:duration)"
                ),
                {
                    "id": asset_id,
                    "key": f"sealed/media/{kind}/fixtures/{asset_id}",
                    "kind": kind,
                    "mime": "image/png" if kind == "images" else "audio/wav",
                    "sha": uuid4().hex * 2,
                    "now": now,
                    "duration": duration,
                    "security": security_status,
                },
            )
        await session.execute(
            text(
                "INSERT INTO audio_target(public_id,stable_key,target_type) VALUES "
                "(:id,:key,'DIALOGUE')"
            ),
            {"id": target, "key": f"scene:{scene}:dialogue"},
        )
        await session.execute(
            text(
                "INSERT INTO "
                "audio_version(public_id,target_id,asset_id,version_no,source,status,created_at) "
                "SELECT :id,t.id,m.id,1,'MANUAL','ACTIVE',:now FROM audio_target t,media_asset m "
                "WHERE t.public_id=:target AND m.public_id=:asset"
            ),
            {"id": version, "target": target, "asset": audio, "now": now},
        )
    content = {
        "title_en": "Hello",
        "title_zh": "你好",
        "original_image_asset_id": image,
        "cover_asset_id": cover,
        "copyright": "Test permission",
        "source": "Test fixture",
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
                "chinese": "你好; 早上好。",
                "start_ms": 0,
                "end_ms": 2800,
                "audio_version_id": version,
                "timing_confirmed": True,
            }
        ],
        "vocabulary": [{"english": "Hello", "chinese": "你好"}],
        "chunks": [{"english": "good morning", "chinese": "早上好"}],
    }
    saved = await service.save_revision(draft.id, content, expected_version=1, actor_id="test")
    assert saved.stable_sentence_ids == (content["dialogue"][0]["id"],)
    checks = await store.checks(saved.id)
    assert all(check.passed for check in checks)
    imported = await store.import_images(
        series["id"], "dialogue", [image, image], "test", slug + "-import"
    )
    assert imported == {"scene_ids": [scene], "reused_scene_ids": [scene]}
    assert (
        await store.import_images(
            series["id"], "dialogue", [image, image], "test", slug + "-import"
        )
        == imported
    )
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE media_asset SET security_status='FAILED' WHERE public_id=:id"),
            {"id": image},
        )
    with pytest.raises(AppError) as blocked:
        await store.publish(saved, "test", slug, now)
    assert blocked.value.code == "PUBLISH_CHECK_FAILED"
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE media_asset SET security_status=:security WHERE public_id=:id"),
            {"id": image, "security": security_status},
        )
    published = await service.publish_revision(saved.id, "test", slug, now, expected_version=2)
    assert published == await service.publish_revision(
        saved.id, "test", slug, now, expected_version=2
    )
    preview_repository = SQLAlchemyContentRepository(sessions, require_review=require_review)
    await preview_repository.save_preview_config(PreviewConfig(series["id"], (scene,), now, "test"))
    preview = await preview_repository.get_preview_scene(scene)
    assert preview is not None and preview["cover_object_key"]
    if not require_review:
        restored_repository = SQLAlchemyContentRepository(sessions, require_review=True)
        with pytest.raises(AppError):
            await restored_repository.get_preview_scene(scene)
    snapshot = await store.full_scene(scene)
    assert snapshot["revision_id"] == saved.id
    word = SceneEntry.model_validate(snapshot["content"]["vocabulary"][0])
    original_word_version = word.entry_version
    word.chinese = "Updated " + uuid4().hex[:6]
    updated = await store.write_entry(word, "VOCABULARY", "test")
    assert updated.entry_version == original_word_version + 1
    assert (await store.full_scene(scene))["content"]["vocabulary"][0][
        "entry_version"
    ] == original_word_version
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE audio_target SET active_version_id=NULL WHERE public_id=:id"),
            {"id": target},
        )
    assert (await store.resource(scene, saved.id, target, published=True))["public_id"] == audio
    assert (await store.resource(scene, saved.id, version, published=True))["public_id"] == audio
    if not require_review:
        restored_review = ProductionStore(sessions, require_review=True)
        with pytest.raises(AppError):
            await restored_review.resource(scene, saved.id, version, published=True)
    with pytest.raises(AppError, match="资源"):
        await store.resource(scene, saved.id, "unrelated", published=True)
    next_draft = await service.create_revision(scene, saved.id, "test", now)
    outcomes = await asyncio.gather(
        *[
            service.save_revision(
                next_draft.id, next_draft.content, expected_version=1, actor_id="test"
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, AppError) for outcome in outcomes) == 1
    history = await service.list_revision_history(scene, page=1, page_size=1)
    assert history["total"] == 2
    assert len(history["items"]) == 1
    assert (await service.get_scene(scene)).template_type == "dialogue"
    second = await service.get_revision(next_draft.id)
    second.content["title_en"] = "New complete version"
    second = await service.save_revision(
        second.id, second.content, expected_version=second.version, actor_id="test"
    )
    await service.publish_revision(
        second.id, "test", slug + "-second", now, expected_version=second.version
    )
    rollback = await service.create_revision(scene, saved.id, "test", now)
    assert rollback.content == saved.content
    assert rollback.stable_sentence_ids == saved.stable_sentence_ids
    assert rollback.stable_entry_ids == saved.stable_entry_ids
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE media_asset SET security_status='FAILED' WHERE public_id=:id"),
            {"id": image},
        )
    with pytest.raises(AppError):
        await service.publish_revision(
            rollback.id, "test", slug + "-rollback", now, expected_version=1
        )
    assert (await store.full_scene(scene))["revision_id"] == second.id
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE media_asset SET security_status=:security WHERE public_id=:id"),
            {"id": image, "security": security_status},
        )
    await service.publish_revision(rollback.id, "test", slug + "-rollback", now, expected_version=1)
    assert (await store.full_scene(scene))["content"] == saved.content
    assert (await store.full_scene(scene))["revision_id"] == rollback.id
    assert (await service.get_revision(second.id)).status == "SUPERSEDED"
    await engine.dispose()
