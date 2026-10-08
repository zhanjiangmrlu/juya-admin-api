import asyncio
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.media.quota import OcrQuotaService, SQLAlchemyOcrQuotaRepository
from juya_admin_api.modules.media.repository import (
    SQLAlchemyMediaAdminRepository,
    SQLAlchemyMediaRepository,
)
from juya_admin_api.modules.media.service import MediaAdminService, MediaAsset
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


@pytest.fixture(scope="module")
def mysql_url() -> str:
    # 功能:读取测试库 URL 并先执行迁移,未配置时跳过用例。
    # 参数:无。
    # 返回:字符串。
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL test URL required")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    return url


@pytest.mark.asyncio
async def test_mysql_metadata_and_single_claim_and_concurrent_quota(mysql_url: str) -> None:
    # 功能:验证 MySQL 媒体元数据、单次认领及并发额度。
    # 参数:
    #     mysql_url: 已执行迁移的独立 MySQL 测试库连接 URL。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    now = datetime.now(UTC)
    engine = create_async_engine(mysql_url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assets = SQLAlchemyMediaRepository(sessions)
        identifier = new_ulid(now)
        image = MediaAsset(
            identifier,
            f"uploads/images/admin/fixtures/{identifier}.png",
            "images",
            "image/png",
            150,
            hashlib.sha256(identifier.encode()).hexdigest(),
            "CONFIRMED",
            "PASSED",
            "admin",
            now,
            width=100,
            height=200,
        )
        await assets.save(image)
        stored = await assets.get(identifier)
        assert stored is not None and (stored.width, stored.height) == (100, 200)
        repository = SQLAlchemyMediaAdminRepository(sessions)
        admin = MediaAdminService(repository)
        job = await admin.create_job(
            business_key=f"claim:{identifier}",
            job_type="OCR",
            target_id=identifier,
            actor_id="admin",
            now=now,
        )
        claims = await asyncio.gather(*(repository.claim_job(job.id, now) for _ in range(6)))
        assert claims.count(True) == 1
        quota = OcrQuotaService(SQLAlchemyOcrQuotaRepository(sessions))
        used = int((await quota.status(now))["reserved_count"])
        await quota.configure(
            enabled=True,
            monthly_limit=used + 3,
            free_quota=used + 3,
            paid_disabled=True,
            verify_quota=True,
            actor_id="admin",
            now=now,
        )
        jobs = [new_ulid(now) for _ in range(8)]
        outcomes = await asyncio.gather(
            *(quota.reserve(item, now) for item in jobs), return_exceptions=True
        )
        assert sum(item is None for item in outcomes) == 3
        assert (
            sum(
                isinstance(item, AppError) and item.code == "OCR_QUOTA_EXHAUSTED"
                for item in outcomes
            )
            == 5
        )
        winner = jobs[outcomes.index(None)]
        await quota.reserve(winner, now)
        assert (await quota.status(now))["reserved_count"] == used + 3
        await quota.configure(
            enabled=False,
            monthly_limit=used + 3,
            free_quota=used + 3,
            paid_disabled=True,
            verify_quota=False,
            actor_id="admin",
            now=now,
        )
    finally:
        await engine.dispose()
