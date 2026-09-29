from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.system_config.service import SQLAlchemySystemConfigRepository


class FakeResult:
    rowcount = 1

    def all(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                config_key="feedback_sla_hours",
                value='{"value": 24}',
                version=1,
            )
        ]


class FakeSession:
    def __init__(self) -> None:
        self.parameters: dict[str, object] | None = None

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> "FakeSession":
        return self

    async def execute(
        self, _statement: object, parameters: dict[str, object] | None = None
    ) -> FakeResult:
        self.parameters = parameters
        return FakeResult()


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


@pytest.mark.asyncio
async def test_repository_decodes_json_text_returned_by_mysql() -> None:
    sessions = cast(async_sessionmaker[AsyncSession], cast(Any, FakeSessionFactory()))
    repository = SQLAlchemySystemConfigRepository(sessions)

    configs = await repository.list_configs()

    assert configs[0].value == {"value": 24}


@pytest.mark.asyncio
async def test_repository_serializes_config_value_for_mysql_json_column() -> None:
    factory = FakeSessionFactory()
    sessions = cast(async_sessionmaker[AsyncSession], cast(Any, factory))
    repository = SQLAlchemySystemConfigRepository(sessions)

    updated = await repository.update_config(
        "shadowing_enabled",
        {"value": True},
        3,
        "admin-1",
    )

    assert updated is not None
    assert factory.session.parameters == {
        "key": "shadowing_enabled",
        "value": '{"value": true}',
        "expected_version": 3,
        "operator_id": "admin-1",
    }
