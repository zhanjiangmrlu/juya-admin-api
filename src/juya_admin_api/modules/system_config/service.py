import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class SystemConfig:
    key: str
    value: dict[str, object]
    version: int


class SystemConfigRepository(Protocol):
    async def list_configs(self) -> list[SystemConfig]: ...

    async def update_config(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig | None: ...


class SystemConfigService:
    def __init__(self, repository: SystemConfigRepository) -> None:
        self._repository = repository

    async def list(self) -> list[SystemConfig]:
        return await self._repository.list_configs()

    async def update(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig:
        updated = await self._repository.update_config(key, value, expected_version, operator_id)
        if updated is None:
            raise AppError(
                "CONFIG_VERSION_CONFLICT",
                "配置已被其他操作更新, 请刷新后重试",
                409,
            )
        return updated


class InMemorySystemConfigRepository:
    def __init__(self, values: dict[str, tuple[dict[str, object], int]]) -> None:
        self._values = deepcopy(values)
        self._lock = asyncio.Lock()

    async def list_configs(self) -> list[SystemConfig]:
        return [
            SystemConfig(key, deepcopy(value), version)
            for key, (value, version) in sorted(self._values.items())
        ]

    async def update_config(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig | None:
        del operator_id
        async with self._lock:
            current = self._values.get(key)
            if current is None or current[1] != expected_version:
                return None
            updated = SystemConfig(key, deepcopy(value), expected_version + 1)
            self._values[key] = (deepcopy(value), updated.version)
            return updated
