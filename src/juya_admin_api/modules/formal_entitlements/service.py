from datetime import datetime

from juya_admin_api.modules.formal_entitlements.domain import (
    FormalEntitlement,
    FormalEntitlementCommand,
    calculate_operation,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    FormalEntitlementRepository,
)


class FormalEntitlementService:
    def __init__(self, repository: FormalEntitlementRepository) -> None:
        # 功能: 初始化正式权益对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供正式权益持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

    async def preview_operation(
        self, command: FormalEntitlementCommand, now: datetime
    ) -> FormalEntitlement:
        # 功能: 计算权益操作结果供管理员预览,不持久化变更.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     command: 正式权益操作命令,包含用户,套餐,操作及期限.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的正式权益状态.
        current = await self._repository.get(command.user_id, command.package_id)
        return calculate_operation(current, command, now)

    async def apply_operation(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
    ) -> FormalEntitlement:
        # 功能: 执行权益命令并持久化幂等结果和审计记录.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     command: 正式权益操作命令,包含用户,套餐,操作及期限.
        #     actor: 执行权益命令的管理员标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 计算或持久化后的正式权益状态.
        return await self._repository.apply(
            command,
            actor,
            idempotency_key,
            now,
            calculate_operation,
        )
