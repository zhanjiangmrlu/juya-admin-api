import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.media.domain import AudioVersion, BatchJobItem
from juya_admin_api.modules.media.repository import SQLAlchemyMediaAdminRepository
from juya_admin_api.modules.media.service import InMemoryMediaAdminRepository, MediaAdminService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_service() -> tuple[MediaAdminService, InMemoryMediaAdminRepository]:
    # 功能:创建当前用例所需业务服务及内存仓库。
    # 参数:无。
    # 返回:tuple[MediaAdminService, InMemoryMediaAdminRepository],由本用例预设的数据或所组装的测
    #       试资源构成。
    repository = InMemoryMediaAdminRepository()
    return MediaAdminService(repository), repository


@pytest.mark.asyncio
async def test_job_and_batch_business_keys_are_idempotent_and_items_are_isolated() -> None:
    # 功能:验证任务和批次业务键幂等且批次条目互相隔离。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, repository = make_service()

    first_job = await service.create_job(
        business_key="ocr:asset-1",
        job_type="OCR",
        target_id="asset-1",
        actor_id="admin-1",
        now=NOW,
    )
    replayed_job = await service.create_job(
        business_key="ocr:asset-1",
        job_type="OCR",
        target_id="asset-ignored",
        actor_id="admin-1",
        now=NOW,
    )
    assert replayed_job == first_job
    assert len(repository.jobs) == 1

    batch = await service.create_batch(
        business_key="ocr-batch:1",
        job_type="OCR",
        target_ids=("asset-1", "asset-2", "asset-3"),
        actor_id="admin-1",
        now=NOW,
    )
    replayed_batch = await service.create_batch(
        business_key="ocr-batch:1",
        job_type="OCR",
        target_ids=("asset-ignored",),
        actor_id="admin-1",
        now=NOW,
    )
    assert replayed_batch.id == batch.id

    await service.finish_batch_item(
        batch.id,
        "0:asset-1",
        succeeded=True,
        error_code=None,
        result_version=1,
        now=NOW,
    )
    await service.finish_batch_item(
        batch.id,
        "1:asset-2",
        succeeded=False,
        error_code="OCR_PROVIDER_FAILED",
        result_version=None,
        now=NOW,
    )

    refreshed = await service.get_batch(batch.id)
    items = await service.list_batch_items(batch.id)
    assert (refreshed.success_count, refreshed.failure_count) == (1, 1)
    assert refreshed.status == "RUNNING"
    assert [(item.item_key, item.status) for item in items] == [
        ("0:asset-1", "SUCCEEDED"),
        ("1:asset-2", "FAILED"),
        ("2:asset-3", "PENDING"),
    ]

    await service.finish_batch_item(
        batch.id,
        "2:asset-3",
        succeeded=True,
        error_code=None,
        result_version=1,
        now=NOW,
    )
    completed = await service.get_batch(batch.id)
    assert completed.status == "COMPLETED_WITH_ERRORS"
    assert (completed.success_count, completed.failure_count) == (2, 1)


@pytest.mark.asyncio
async def test_generated_audio_replay_is_idempotent_and_never_replaces_manual_active_version() -> (
    None
):
    # 功能:验证生成音频重放幂等且不替换人工激活版本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, repository = make_service()

    generated = await service.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="asset-tts-1",
        source="TTS",
        actor_id="admin-1",
        now=NOW,
        provider_request_id="tts-request-1",
    )
    replayed = await service.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="asset-tts-duplicate",
        source="TTS",
        actor_id="admin-1",
        now=NOW,
        provider_request_id="tts-request-1",
    )
    assert replayed == generated

    manual = await service.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="asset-manual-1",
        source="MANUAL",
        actor_id="admin-1",
        now=NOW + timedelta(seconds=1),
    )
    active = await service.confirm_audio_version(manual.id, actor_id="admin-1", now=NOW)

    retry_result = await service.create_audio_candidate(
        stable_key="sentence-1",
        target_type="SENTENCE",
        asset_id="asset-tts-2",
        source="TTS",
        actor_id="system",
        now=NOW + timedelta(seconds=2),
        provider_request_id="tts-request-2",
    )

    target = await service.get_audio_target(active.id)
    versions = await service.list_audio_versions(active.id)
    assert target.active_version_id == manual.id
    assert retry_result.status == "CANDIDATE"
    assert len(versions) == 3
    assert len(repository.audio_versions) == 3


@pytest.mark.asyncio
async def test_audio_version_can_roll_back_to_an_older_confirmed_version() -> None:
    # 功能:验证音频可回滚到更早的已确认版本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, _ = make_service()

    first = await service.create_audio_candidate(
        stable_key="sentence-2",
        target_type="SENTENCE",
        asset_id="asset-manual-1",
        source="MANUAL",
        actor_id="admin-1",
        now=NOW,
    )
    target = await service.confirm_audio_version(first.id, actor_id="admin-1", now=NOW)
    second = await service.create_audio_candidate(
        stable_key="sentence-2",
        target_type="SENTENCE",
        asset_id="asset-manual-2",
        source="MANUAL",
        actor_id="admin-1",
        now=NOW + timedelta(seconds=1),
    )
    await service.confirm_audio_version(second.id, actor_id="admin-1", now=NOW)

    rolled_back = await service.rollback_audio_version(
        target.id,
        first.id,
        actor_id="admin-1",
        now=NOW + timedelta(seconds=2),
    )
    versions = await service.list_audio_versions(target.id)

    assert rolled_back.active_version_id == first.id
    assert {version.id: version.status for version in versions} == {
        first.id: "ACTIVE",
        second.id: "SUPERSEDED",
    }


@pytest.mark.asyncio
async def test_trash_cleanup_observes_retention_and_reference_protection() -> None:
    # 功能:验证回收站清理遵循保留期及引用保护。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    service, repository = make_service()
    repository.register_draft("scene-1", "revision-1")

    trashed = await service.trash_draft("scene-1", "revision-1", actor_id="admin-1", now=NOW)
    with pytest.raises(AppError) as retention_error:
        await service.cleanup_draft(trashed.id, actor_id="admin-1", now=NOW)
    assert retention_error.value.code == "TRASH_RETENTION_ACTIVE"

    repository.set_draft_referenced("revision-1", True)
    with pytest.raises(AppError) as reference_error:
        await service.cleanup_draft(
            trashed.id,
            actor_id="admin-1",
            now=NOW + timedelta(days=31),
        )
    assert reference_error.value.code == "DRAFT_REFERENCED"
    assert (await service.get_trash_entry(trashed.id)).status == "TRASHED"

    repository.set_draft_referenced("revision-1", False)
    cleaned = await service.cleanup_draft(
        trashed.id,
        actor_id="admin-1",
        now=NOW + timedelta(days=31),
    )
    assert cleaned.status == "CLEANED"


@pytest.fixture(scope="module")
def mysql_url() -> str:
    # 功能:读取测试库 URL 并先执行迁移,未配置时跳过用例。
    # 参数:无。
    # 返回:字符串。
    database_url = os.getenv("JUYA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("JUYA_TEST_DATABASE_URL is required for MySQL integration tests")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    return database_url


def test_media_admin_migration_adds_job_audio_and_trash_guards(mysql_url: str) -> None:
    # 功能:验证媒体管理迁移新增任务、音频和回收站约束。
    # 参数:
    #     mysql_url: 已执行迁移的独立 MySQL 测试库连接 URL。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from sqlalchemy import create_engine

    engine = create_engine(mysql_url)
    try:
        inspector = inspect(engine)
        assert {"processing_job", "draft_trash"}.issubset(inspector.get_table_names())
        processing_indexes = {index["name"] for index in inspector.get_indexes("processing_job")}
        assert "uq_processing_job_business_key" in processing_indexes
        assert "ix_processing_job_admin" in processing_indexes

        audio_columns = {column["name"] for column in inspector.get_columns("audio_version")}
        assert {"processing_job_id", "created_by"}.issubset(audio_columns)
        ocr_columns = {column["name"] for column in inspector.get_columns("ocr_candidate")}
        assert {"processing_job_id", "confirmed_revision_id", "confirmed_by"}.issubset(ocr_columns)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version FROM schema_version WHERE id = 1")) >= 13
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_sql_repository_replays_business_keys_and_keeps_batch_results(mysql_url: str) -> None:
    # 功能:验证 SQL 仓库按业务键重放且保留批处理结果。
    # 参数:
    #     mysql_url: 已执行迁移的独立 MySQL 测试库连接 URL。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    async_url = mysql_url.replace("mysql+pymysql://", "mysql+asyncmy://", 1)
    engine = create_async_engine(async_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SQLAlchemyMediaAdminRepository(sessions)
    service = MediaAdminService(repository)
    suffix = NOW.strftime("%Y%m%d%H%M%S")

    try:
        first = await service.create_job(
            business_key=f"test:ocr:{suffix}",
            job_type="OCR",
            target_id="asset-1",
            actor_id="admin-1",
            now=NOW,
        )
        replayed = await service.create_job(
            business_key=f"test:ocr:{suffix}",
            job_type="OCR",
            target_id="asset-ignored",
            actor_id="admin-1",
            now=NOW,
        )
        assert replayed.id == first.id

        batch = await service.create_batch(
            business_key=f"test:batch:{suffix}",
            job_type="VALIDATE",
            target_ids=("scene-1", "scene-2"),
            actor_id="admin-1",
            now=NOW,
        )
        await service.finish_batch_item(
            batch.id,
            "0:scene-1",
            succeeded=True,
            error_code=None,
            result_version=1,
            now=NOW,
        )
        await service.finish_batch_item(
            batch.id,
            "1:scene-2",
            succeeded=False,
            error_code="VALIDATION_FAILED",
            result_version=None,
            now=NOW,
        )
        persisted = await service.get_batch(batch.id)
        assert isinstance((await service.list_batch_items(batch.id))[0], BatchJobItem)
        assert persisted.status == "COMPLETED_WITH_ERRORS"
        assert (persisted.success_count, persisted.failure_count) == (1, 1)
    finally:
        await engine.dispose()


def test_audio_version_domain_keeps_only_object_identifiers() -> None:
    # 功能:验证音频版本领域对象只保存对象标识。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    fields = set(AudioVersion.__dataclass_fields__)
    assert "object_key" not in fields
    assert "url" not in fields
    assert "asset_id" in fields
