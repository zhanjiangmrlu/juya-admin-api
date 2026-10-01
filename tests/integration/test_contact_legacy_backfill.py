import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks.maintenance import _aggregate_daily


@pytest.mark.asyncio
async def test_legacy_contact_first_conversion_is_deduplicated_across_completed_days() -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    engine = create_engine(url)
    suffix = uuid4().hex[:16]
    user = "LEGACY" + suffix
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status,created_at) "
                    "VALUES(:user,:number,'ACTIVE',UTC_TIMESTAMP(6))"
                ),
                {"user": user, "number": suffix},
            )
            for index, day in enumerate([1, 2, 2]):
                connection.execute(
                    text(
                        "INSERT INTO analytics_event(id,event_key,user_id,event_type,occurred_at,"
                        "dimension,payload) SELECT :event,:key,id,:type,:at,'ALL',JSON_OBJECT() "
                        "FROM user_account WHERE public_id=:user"
                    ),
                    {
                        "event": suffix + str(index),
                        "key": suffix + str(index),
                        "user": user,
                        "type": "CONTACT_CHANGED" if index == 2 else "CONTACT_SUBMITTED",
                        "at": datetime(2011, 1, day, 3, tzinfo=UTC),
                    },
                )
        settings = Settings(database_url=SecretStr(url))
        for _ in range(2):
            await _aggregate_daily(settings, metric_day=date(2011, 1, 1))
            await _aggregate_daily(settings, metric_day=date(2011, 1, 2))
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT metric_day,metric,metric_value FROM analytics_daily "
                    "WHERE metric_day IN ('2011-01-01','2011-01-02') AND dimension='ALL' "
                    "AND metric IN ('CONTACT_SUBMISSIONS','CONTACT_CHANGES')"
                )
            ).all()
        assert (date(2011, 1, 1), "CONTACT_SUBMISSIONS", 1) in rows
        assert (date(2011, 1, 2), "CONTACT_SUBMISSIONS", 0) in rows
        assert (date(2011, 1, 2), "CONTACT_CHANGES", 1) in rows
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM analytics_event WHERE event_key LIKE :key"), {"key": suffix + "%"}
            )
            connection.execute(text("DELETE FROM user_account WHERE public_id=:id"), {"id": user})
            connection.execute(
                text("DELETE FROM analytics_daily WHERE metric_day IN ('2011-01-01','2011-01-02')")
            )
        engine.dispose()
