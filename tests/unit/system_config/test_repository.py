from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.system_config.service import SQLAlchemySystemConfigRepository


class FakeResult:
    rowcount = 1

    def all(self) -> list[SimpleNamespace]:
        # 功能:返回模拟 SQL 查询结果的全部记录。
        # 参数:
        #     self: 当前 FakeResult 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:list[SimpleNamespace],由本用例预设的数据或所组装的测试资源构成。
        return [
            SimpleNamespace(
                config_key="feedback_sla_hours",
                value='{"value": 24}',
                version=1,
            )
        ]


class FakeSession:
    def __init__(self) -> None:
        # 功能:初始化 FakeSession 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeSession 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.parameters: dict[str, object] | None = None

    async def __aenter__(self) -> "FakeSession":
        # 功能:进入模拟异步会话或事务并返回当前对象。
        # 参数:
        #     self: 当前 FakeSession 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:'FakeSession',由本用例预设的数据或所组装的测试资源构成。
        return self

    async def __aexit__(self, *_args: object) -> None:
        # 功能:退出模拟异步上下文并更新测试记录。
        # 参数:
        #     self: 当前 FakeSession 测试替身实例,保存本用例的预设状态或调用记录。
        #     _args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。 当前替身保留
        #       该形参以兼容调用接口。
        # 返回:None,由本用例预设的数据或所组装的测试资源构成。
        return None

    def begin(self) -> "FakeSession":
        # 功能:返回模拟数据库事务上下文。
        # 参数:
        #     self: 当前 FakeSession 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:'FakeSession',由本用例预设的数据或所组装的测试资源构成。
        return self

    async def execute(
        self, _statement: object, parameters: dict[str, object] | None = None
    ) -> FakeResult:
        # 功能:模拟 SQL 执行并返回预设结果或更新内存配置。
        # 参数:
        #     self: 当前 FakeSession 测试替身实例,保存本用例的预设状态或调用记录。
        #     _statement: 即将执行的 SQL 语句,用于选择故障注入位置。 当前替身保留该形参以兼容调
        #       用接口。
        #     parameters: SQL 执行的绑定参数,供模拟仓库更新配置或记录调用。
        # 返回:FakeResult,由本用例预设的数据或所组装的测试资源构成。
        self.parameters = parameters
        return FakeResult()


class FakeSessionFactory:
    def __init__(self) -> None:
        # 功能:初始化 FakeSessionFactory 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 FakeSessionFactory 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        # 功能:通过可调用替身构造新的模拟数据库会话。
        # 参数:
        #     self: 当前 FakeSessionFactory 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:FakeSession,由本用例预设的数据或所组装的测试资源构成。
        return self.session


@pytest.mark.asyncio
async def test_repository_decodes_json_text_returned_by_mysql() -> None:
    # 功能:验证仓库解码 MySQL 返回的 JSON 文本。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    sessions = cast(async_sessionmaker[AsyncSession], cast(Any, FakeSessionFactory()))
    repository = SQLAlchemySystemConfigRepository(sessions)

    configs = await repository.list_configs()

    assert configs[0].value == {"value": 24}


@pytest.mark.asyncio
async def test_repository_serializes_config_value_for_mysql_json_column() -> None:
    # 功能:验证仓库为 MySQL JSON 列序列化配置值。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
