import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class SystemConfig:
    key: str
    value: dict[str, object]
    version: int


class SystemConfigRepository(Protocol):
    async def list_configs(self) -> list[SystemConfig]:
        # 功能: 读取全部系统配置及版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 按配置键排序的系统配置和版本列表.
        ...

    async def update_config(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig | None:
        # 功能: 按预期版本更新系统配置并返回新版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     value: 系统配置内容字典,受控整数保存在 value 字段中.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     operator_id: 更新配置的管理员标识.
        # 返回: 更新后的配置;不存在或版本冲突时为 None.
        ...


class SystemConfigService:
    def __init__(self, repository: SystemConfigRepository) -> None:
        # 功能: 初始化系统配置对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供系统配置持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

    async def list(self) -> list[SystemConfig]:
        # 功能: 读取全部系统配置及版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 按配置键排序的系统配置和版本列表.
        return await self._repository.list_configs()

    async def integer(self, key: str, default: int) -> int:
        # 功能: 读取整数系统配置并校验允许范围.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     default: 目标配置缺失时采用的回退值.
        # 返回: 通过范围校验的整数配置.
        configs = await self.list()
        value = next((item.value.get("value") for item in configs if item.key == key), default)
        self._validate(key, {"value": value})
        return int(cast(int, value))

    async def feedback_sla_hours(self) -> int:
        # 功能: 读取反馈响应时限小时数,缺失时采用 48 小时.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 反馈响应时限,单位为小时.
        return await self.integer("feedback_sla_hours", 48)

    async def entitlement_warning_days(self) -> int:
        # 功能: 读取权益到期预警天数,缺失时采用 30 天.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 权益到期预警长度,单位为天.
        return await self.integer("entitlement_expiry_warning_days", 30)

    @staticmethod
    def _validate(key: str, value: dict[str, object]) -> None:
        # 功能: 检查受控系统配置的整数类型及允许范围.
        # 参数:
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     value: 系统配置内容字典,受控整数保存在 value 字段中.
        # 返回: 无返回值;正常完成表示本次操作成功.
        ranges = {"feedback_sla_hours": (1, 720), "entitlement_expiry_warning_days": (1, 365)}
        if key in ranges:
            number = value.get("value")
            low, high = ranges[key]
            if type(number) is not int or not low <= number <= high:
                raise AppError("CONFIG_VALUE_INVALID", "配置必须为允许范围内的整数", 422)

    async def update(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig:
        # 功能: 校验配置值后按版本更新系统配置,冲突时返回业务错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     value: 系统配置内容字典,受控整数保存在 value 字段中.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     operator_id: 更新配置的管理员标识.
        # 返回: 更新后的配置值和版本.
        self._validate(key, value)
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
        # 功能: 初始化系统配置对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     values: 初始化内存配置仓储的配置键到内容和版本的映射.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._values = deepcopy(values)
        self._lock = asyncio.Lock()

    async def list_configs(self) -> list[SystemConfig]:
        # 功能: 读取全部系统配置及版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 按配置键排序的系统配置和版本列表.
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
        # 功能: 按预期版本更新系统配置并返回新版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     value: 系统配置内容字典,受控整数保存在 value 字段中.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     operator_id: 更新配置的管理员标识.
        # 返回: 更新后的配置;不存在或版本冲突时为 None.
        del operator_id
        async with self._lock:
            current = self._values.get(key)
            if current is None or current[1] != expected_version:
                return None
            updated = SystemConfig(key, deepcopy(value), expected_version + 1)
            self._values[key] = (deepcopy(value), updated.version)
            return updated


class SQLAlchemySystemConfigRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # 功能: 初始化系统配置对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     session_factory: 创建 SQLAlchemy 异步会话的工厂,每次操作独立管理事务.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._session_factory = session_factory

    async def list_configs(self) -> list[SystemConfig]:
        # 功能: 读取全部系统配置及版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 按配置键排序的系统配置和版本列表.
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text("SELECT config_key, value, version FROM system_config ORDER BY config_key")
                )
            ).all()
        return [
            SystemConfig(row.config_key, _decode_config_value(row.value), row.version)
            for row in rows
        ]

    async def update_config(
        self,
        key: str,
        value: dict[str, object],
        expected_version: int,
        operator_id: str,
    ) -> SystemConfig | None:
        # 功能: 按预期版本更新系统配置并返回新版本.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     key: 系统配置项名称,例如 feedback_sla_hours.
        #     value: 系统配置内容字典,受控整数保存在 value 字段中.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     operator_id: 更新配置的管理员标识.
        # 返回: 更新后的配置;不存在或版本冲突时为 None.
        async with self._session_factory() as session, session.begin():
            result = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE system_config SET value = :value, version = version + 1, "
                        "updated_by = :operator_id, updated_at = UTC_TIMESTAMP(6) "
                        "WHERE config_key = :key AND version = :expected_version"
                    ),
                    {
                        "key": key,
                        "value": json.dumps(value, ensure_ascii=False),
                        "expected_version": expected_version,
                        "operator_id": operator_id,
                    },
                ),
            )
            if result.rowcount != 1:
                return None
        return SystemConfig(key, deepcopy(value), expected_version + 1)


def _decode_config_value(value: object) -> dict[str, object]:
    # 功能: 解析数据库系统配置,拒绝非 JSON 对象结构.
    # 参数:
    #     value: 数据库存储的系统配置 JSON 文本或字典.
    # 返回: 解析并确认结构后的系统配置字典.
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("system_config.value must contain a JSON object")
    return cast(dict[str, object], decoded)
