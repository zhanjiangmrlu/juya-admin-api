import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
@pytest.mark.parametrize("run_hour", [0, 1, 2])
async def test_one_am_aggregates_completed_shanghai_day_and_keeps_today_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    run_hour: int,
) -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL URL is required for daily producer acceptance")
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    # 2035-01-01 01:00 in Shanghai: the previous full day is 2034-12-31.
    now = datetime(2034, 12, 31, 16 + run_hour, tzinfo=UTC)

    class FixedClock(datetime):
        @classmethod
        def now(cls, _tz: object = None) -> datetime:
            return now

    monkeypatch.setattr(maintenance, "datetime", FixedClock)
    user_ids = [new_ulid(now) for _ in range(3)]
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            # Noon and 23:30 of Dec31 must count; 00:30 of Jan1 must not.
            for user_id, created_at in zip(
                user_ids,
                [
                    datetime(2034, 12, 31, 4),
                    datetime(2034, 12, 31, 15, 30),
                    datetime(2034, 12, 31, 16, 30),
                ],
                strict=True,
            ):
                connection.execute(
                    text(
                        "INSERT INTO user_account (public_id, juya_number, status, created_at) "
                        "VALUES (:id, :number, 'ACTIVE', :created)"
                    ),
                    {"id": user_id, "number": f"B6{user_id[-12:]}", "created": created_at},
                )
                connection.execute(
                    text(
                        "INSERT INTO analytics_event "
                        "(id,event_key,user_id,event_type,occurred_at,dimension,payload) "
                        "SELECT :event,:key,id,'USER_CREATED',:created,'ALL',JSON_OBJECT() "
                        "FROM user_account WHERE public_id=:user"
                    ),
                    {
                        "event": new_ulid(now),
                        "key": "seed-user:" + user_id,
                        "created": created_at,
                        "user": user_id,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO analytics_daily "
                    "(metric_day, metric, dimension, metric_value, generated_at) "
                    "VALUES ('2034-12-31','CONTACT_FUNNEL','NUMERATOR',4,UTC_TIMESTAMP(6)), "
                    "('2034-12-31','CONTACT_FUNNEL','DENOMINATOR',5,UTC_TIMESTAMP(6)), "
                    "('2034-12-31','ACTIVE_USERS','ALL',10,UTC_TIMESTAMP(6))"
                )
            )
        result = await maintenance._aggregate_daily(Settings(database_url=SecretStr(url)))
        assert result["day"] == "2034-12-31"
        assert result["metric_count"] >= 30
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT metric_day, metric, dimension, metric_value FROM analytics_daily "
                    "WHERE metric_day IN ('2034-12-31','2035-01-01')"
                )
            ).all()
        assert (date(2034, 12, 31), "NEW_USERS", "ALL", 2) in rows
        assert any(
            day == date(2035, 1, 1) and metric == "CONTACT_STATES"
            for day, metric, _dimension, _value in rows
        )
        assert (date(2034, 12, 31), "CONTACT_FUNNEL", "NUMERATOR", 0) in rows
        assert (date(2034, 12, 31), "ACTIVE_USERS", "ALL", 0) in rows
        backfilled = await maintenance._aggregate_daily(
            Settings(database_url=SecretStr(url)), metric_day=date(2034, 12, 31)
        )
        assert backfilled["metric_count"] >= 30
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM analytics_daily WHERE metric_day IN ('2034-12-31','2035-01-01')")
            )
            for user_id in user_ids:
                connection.execute(
                    text("DELETE FROM analytics_event WHERE event_key=:key"),
                    {"key": "seed-user:" + user_id},
                )
                connection.execute(
                    text("DELETE FROM user_account WHERE public_id=:id"), {"id": user_id}
                )
        engine.dispose()
