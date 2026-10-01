import os
from datetime import UTC, date, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.infrastructure.tasks import maintenance
from juya_admin_api.modules.analytics.events import append_event
from juya_admin_api.modules.analytics.service import (
    SQLAlchemyAnalyticsRepository,
    query_aggregate_rows,
)
from juya_admin_api.shared.ids import new_ulid


@pytest.mark.asyncio
async def test_real_events_produce_privacy_safe_cohort_rates_modes_and_weighted_response_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required")
    now = datetime(2040, 1, 3, 3, tzinfo=UTC)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(maintenance, "datetime", FixedClock)
    engine = create_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    factory = create_session_factory(engine)
    prefix = "v13-pipeline:" + new_ulid(now)
    day = date(2040, 1, 1)
    one = datetime(2040, 1, 1, 3, tzinfo=UTC)
    two = datetime(2040, 1, 2, 3, tzinfo=UTC)
    records = [
        ("grant", "LIMITED_GRANTED", one, "campaign:test", {"mode": 3}),
        ("start", "LIMITED_STARTED", two, "campaign:test", {"mode": 3, "cohort_day": "2040-01-01"}),
        (
            "done",
            "LIMITED_COMPLETED",
            two,
            "campaign:test",
            {"mode": 3, "started_day": "2040-01-02", "before_expiry": True},
        ),
        ("exposure", "CONTACT_PROMPT_EXPOSED", one, "ALL", {}),
        ("submit", "CONTACT_SUBMITTED", two, "ALL", {"prompted": True, "cohort_day": "2040-01-01"}),
        ("feedback-one", "FEEDBACK_CREATED", one, "FUNCTION", {"category": "FUNCTION"}),
        ("feedback-two", "FEEDBACK_CREATED", one, "CONTENT", {"category": "CONTENT"}),
        (
            "reply-one",
            "FEEDBACK_RESPONDED",
            one,
            "FUNCTION",
            {
                "category": "FUNCTION",
                "response_seconds": 20,
                "created_day": "2040-01-01",
                "before_expiry": True,
            },
        ),
        (
            "reply-two",
            "FEEDBACK_RESPONDED",
            two,
            "CONTENT",
            {
                "category": "CONTENT",
                "response_seconds": 40,
                "created_day": "2040-01-01",
                "before_expiry": True,
            },
        ),
        ("scene-start", "SCENE_STARTED", one, "scene:test", {}),
        ("scene-complete", "SCENE_COMPLETED", one, "scene:test", {}),
        ("favorite", "FAVORITE_CREATED", one, "scene:test", {}),
        ("review", "REVIEW_COMPLETED", one, "ALL", {}),
        ("delete", "DELETION_EFFECTIVE", one, "ALL", {}),
    ]
    try:
        async with factory() as session, session.begin():
            for key, event_type, at, dimension, payload in records:
                assert await append_event(
                    session,
                    event_key=prefix + key,
                    event_type=event_type,
                    user_id=None,
                    occurred_at=at,
                    dimension=dimension,
                    payload=payload,
                )
            assert not await append_event(
                session,
                event_key=prefix + "grant",
                event_type="LIMITED_GRANTED",
                user_id=None,
                occurred_at=one,
                payload={"mode": 3},
            )
        settings = Settings(database_url=SecretStr(url))
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO analytics_daily "
                    "(metric_day,metric,dimension,metric_value,generated_at) "
                    "VALUES('2040-01-01','CONTACT_STATES','CONTACTED',7,UTC_TIMESTAMP(6)) "
                    "ON DUPLICATE KEY UPDATE metric_value=7"
                )
            )
        scheduled = await maintenance._aggregate_daily(settings)
        assert scheduled["refreshed_cohort_days"] == ["2040-01-01"]
        await maintenance._aggregate_daily(settings, metric_day=day)
        repository = SQLAlchemyAnalyticsRepository(factory)
        first = await repository.query(day, day)
        assert any(
            row.metric == "CONTACT_STATES" and row.dimension == "CONTACTED" and row.value == 7
            for row in first
        )
        await maintenance._aggregate_daily(settings, metric_day=day)
        assert await repository.query(day, day) == first
        counts, ratios = query_aggregate_rows(first, "day")
        rates = {(row["metric"], row.get("dimension", "ALL")): row for row in ratios}
        assert rates[("CONTACT_FUNNEL", "ALL")]["rate"] == 1
        assert rates[("LIMITED_STARTS", "MODE_3")]["rate"] == 1
        assert rates[("FEEDBACK_SLA", "ALL")]["rate"] == 1
        assert any(
            row["metric"] == "SCENE_COMPLETIONS"
            and row["dimension"] == "scene:test"
            and row["value"] == 1
            for row in counts
        )
        await maintenance._aggregate_daily(settings, metric_day=date(2040, 1, 2))
        _, monthly = query_aggregate_rows(await repository.query(day, date(2040, 1, 2)), "month")
        duration = next(row for row in monthly if row["metric"] == "FEEDBACK_RESPONSE_SECONDS")
        assert duration["rate"] == 30 and duration["unit"] == "seconds"
        assert all("user_id" not in row and "event_key" not in row for row in counts)
    finally:
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM analytics_event WHERE event_key LIKE :prefix"),
                {"prefix": prefix + "%"},
            )
            await session.execute(
                text(
                    "DELETE FROM analytics_daily WHERE metric_day "
                    "IN ('2040-01-01','2040-01-02','2040-01-03')"
                )
            )
        await engine.dispose()
