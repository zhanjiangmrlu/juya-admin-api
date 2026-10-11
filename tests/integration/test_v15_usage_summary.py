import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.cloud_usage.models import attempts
from juya_admin_api.modules.cloud_usage.router import usage_summary


@pytest.mark.asyncio
async def test_read_only_usage_preserves_unverified_null_and_reserved_unknown_attempts() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    period = "2088-01-01"
    identity = uuid4().hex
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                insert(attempts).values(
                    public_id=identity,
                    stage_id=identity,
                    kind="AI_LEXICON",
                    period=period,
                    reserved_units=200,
                    actual_units=None,
                    status="UNKNOWN",
                    usage={},
                    price_version="UNVERIFIED",
                    region="fixture",
                    currency="CNY",
                    created_at=datetime.now(UTC),
                )
            )
        summary = await usage_summary(sessions, period)
        assert summary["measured_cloud"][0]["actual_units"] is None
        assert summary["measured_cloud"][0]["reserved_units"] == 200
        assert summary["measured_cloud"][0]["unknown_attempts"] == 1
        assert summary["tts"]["verified_actual_microcost"] is None
        assert summary["supplier_invoice_verified"] is False
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM content_cloud_attempt WHERE public_id=:id"),
                    {"id": identity},
                )
                == "UNKNOWN"
            )
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM content_cloud_attempt WHERE public_id=:id"), {"id": identity}
            )
        await engine.dispose()
