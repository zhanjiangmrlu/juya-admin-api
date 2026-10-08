import hashlib
import json
from datetime import datetime
from typing import Any, Protocol

from fastapi.encoders import jsonable_encoder

from juya_admin_api.modules.campaigns.domain import CampaignDuration, CampaignVersion
from juya_admin_api.shared.errors import AppError


class CampaignRepository(Protocol):
    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        # 功能: 按条件读取限时活动列表及分页信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 活动列表及 page,page_size,total 分页字段;各项包含当前版本及可用操作.
        ...

    async def get(self, campaign_id: str) -> dict[str, Any] | None:
        # 功能: 读取指定限时活动记录,不存在时抛出业务错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     不存在时为 None.
        ...

    async def save(
        self,
        campaign_id: str | None,
        *,
        expected_version: int | None = None,
        name: str,
        now: datetime,
        duration_days: int | None = None,
        activation_window_days: int | None = None,
        capacity: int | None = None,
        scene_ids: tuple[str, ...] | None = None,
        actor_id: str = "system",
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        # 功能: 创建或更新活动及版本字段,保证版本一致性和请求幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     name: 活动展示名称.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     duration_days: 限时权益激活后的有效天数,只接受 3 或 5 天.
        #     activation_window_days: 开通后允许首次启动的窗口长度,单位为天.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        #     scene_ids: 权益或活动版本绑定的固定场景公开标识集合.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        ...

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        now: datetime,
        capacity: int | None = None,
        actor_id: str = "system",
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        # 功能: 执行活动版本命令,检查状态及版本并记录幂等和审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     request_hash: 规范化业务请求的摘要,用于检测同一幂等键被不同请求复用.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        ...


class CampaignService:
    def __init__(self, repository: CampaignRepository) -> None:
        # 功能: 初始化限时活动对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     repository: 提供限时活动持久化和查询能力的仓储.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._repository = repository

    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        # 功能: 按条件读取限时活动列表及分页信息.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 活动列表及 page,page_size,total 分页字段;各项包含当前版本及可用操作.
        return await self._repository.list(filters, page, page_size)

    async def get(self, campaign_id: str) -> dict[str, Any]:
        # 功能: 读取指定限时活动记录,不存在时抛出业务错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        result = await self._repository.get(campaign_id)
        if result is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "活动不存在", 404)
        return result

    async def save(
        self,
        campaign_id: str | None,
        *,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        **fields: Any,
    ) -> dict[str, Any]:
        # 功能: 创建或更新活动及版本字段,保证版本一致性和请求幂等.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     **fields: 保存活动时展开的名称,期限,容量和场景字段.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        request = {"campaign_id": campaign_id, **fields}
        result = await self._repository.save(
            campaign_id,
            actor_id=actor_id,
            now=now,
            idempotency_key=idempotency_key,
            request_hash=_hash(request),
            **fields,
        )
        body: dict[str, Any] = jsonable_encoder(result)
        return body

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        # 功能: 执行活动版本命令,检查状态及版本并记录幂等和审计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     expected_version: 调用方读取到的版本号,写入时用于检测并发更新.
        #     actor_id: 执行本次操作的主体标识,供审计和幂等隔离使用.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        request = {
            "campaign_id": campaign_id,
            "operation": operation,
            "expected_version": expected_version,
            "capacity": capacity,
        }
        result = await self._repository.command(
            campaign_id,
            operation,
            expected_version=expected_version,
            actor_id=actor_id,
            now=now,
            capacity=capacity,
            idempotency_key=idempotency_key,
            request_hash=_hash(request),
        )
        body: dict[str, Any] = jsonable_encoder(result)
        return body

    @staticmethod
    def revise_version(
        current: CampaignVersion,
        *,
        duration_days: CampaignDuration,
        activation_window_days: int,
        capacity: int,
        scene_ids: tuple[str, ...],
    ) -> CampaignVersion:
        # 功能: 修改允许编辑的活动版本并校验期限,容量及场景限制.
        # 参数:
        #     current: 变更前的活动版本,限制锁定或已关闭版本修改.
        #     duration_days: 限时权益激活后的有效天数,只接受 3 或 5 天.
        #     activation_window_days: 开通后允许首次启动的窗口长度,单位为天.
        #     capacity: 活动版本允许累计开通的用户数量,不能低于已开通人数.
        #     scene_ids: 权益或活动版本绑定的固定场景公开标识集合.
        # 返回: 已校验且版本号递增的活动版本.
        if current.locked_at is not None and (
            duration_days != current.duration_days
            or activation_window_days != current.activation_window_days
            or scene_ids != current.scene_ids
        ):
            raise AppError(
                "CAMPAIGN_VERSION_LOCKED",
                "活动首次开通后时长、启动窗口和场景不可修改",
                409,
            )
        current.duration_days = duration_days
        current.activation_window_days = activation_window_days
        current.capacity = capacity
        current.scene_ids = scene_ids
        current.validate()
        return current


def _hash(request: dict[str, Any]) -> str:
    # 功能: 对规范化活动请求生成 SHA-256 幂等摘要.
    # 参数:
    #     request: 待规范化并计算幂等摘要的活动业务请求字段.
    # 返回: 规范化活动请求的 SHA-256 摘要.
    payload = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
