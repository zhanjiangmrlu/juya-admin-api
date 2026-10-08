from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.router import EntitlementQueryRepository
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.shared.errors import AppError


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    campaign_version_id: str = Field(min_length=1, max_length=64)


class RemedyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RemedyMode


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


AdminDependency = Callable[..., Awaitable[SessionRecord]]


class LimitedEntitlementDetailResponse(BaseModel):
    id: str
    user_id: str
    campaign_version_id: str
    campaign_id: str
    campaign_name: str
    status: str
    granted_at: datetime
    start_deadline: datetime
    activated_at: datetime | None
    expires_at: datetime | None
    remedy_count: int
    version: int
    duration_days: int
    activation_window_days: int
    scene_ids: list[str]
    available_operations: list[str]


def _serialize(entitlement: LimitedEntitlement) -> dict[str, object]:
    # 功能: 将限时权益领域对象转换为接口响应字段.
    # 参数:
    #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
    # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
    return {
        "id": entitlement.id,
        "user_id": entitlement.user_id,
        "campaign_version_id": entitlement.campaign_version_id,
        "status": entitlement.status,
        "granted_at": entitlement.granted_at,
        "start_deadline": entitlement.start_deadline,
        "activated_at": entitlement.activated_at,
        "expires_at": entitlement.expires_at,
        "remedy_count": entitlement.remedy_count,
        "version": entitlement.version,
    }


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_limited_entitlement_router(
    service: LimitedEntitlementService,
    *,
    query_repository: EntitlementQueryRepository,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建限时权益查询,开通,补救及状态操作路由.
    # 参数:
    #     service: 执行业务操作的限时权益服务.
    #     query_repository: 查询正式和限时权益明细及套餐列表的仓储.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     current_admin_write: 同时校验管理员身份和 CSRF 的写操作依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(
        prefix="/api/v1/admin/limited-entitlements",
        tags=["limited-entitlements"],
    )

    @router.get("/{entitlement_id}", response_model=LimitedEntitlementDetailResponse)
    async def get_entitlement(
        entitlement_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        # 功能: 查询指定权益详情,不存在时返回业务错误.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 限时权益及关联活动信息,固定场景标识和可用操作.不存在时抛出业务错误.
        result = await query_repository.get_limited(entitlement_id)
        if result is None:
            raise AppError("LIMITED_ENTITLEMENT_NOT_FOUND", "限时权益不存在", 404)
        return result

    @router.post("/commands/grant", status_code=201)
    async def grant(
        payload: GrantRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 按指定活动版本授予限时权益并控制重复开通和容量.
        # 参数:
        #     payload: 待开通用户和活动版本标识.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
        result = await service.grant(
            payload.user_id,
            payload.campaign_version_id,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/{entitlement_id}/commands/remedy")
    async def remedy(
        entitlement_id: str,
        payload: RemedyRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 执行限时权益启动期限延长或启动窗口恢复.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     payload: 补救模式及原因说明.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
        result = await service.remedy(
            entitlement_id,
            payload.mode,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/{entitlement_id}/commands/pause")
    async def pause(
        entitlement_id: str,
        payload: ReasonRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 暂停仍在有效期内的限时权益.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     payload: 暂停或撤销权益的原因说明.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
        return _serialize(
            await service.pause(
                entitlement_id,
                str(admin.admin_user_id),
                idempotency_key,
                payload.reason,
                clock(),
            )
        )

    @router.post("/{entitlement_id}/commands/resume")
    async def resume(
        entitlement_id: str,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 恢复尚未到期的已暂停限时权益.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
        return _serialize(
            await service.resume(entitlement_id, str(admin.admin_user_id), idempotency_key, clock())
        )

    @router.post("/{entitlement_id}/commands/revoke")
    async def revoke(
        entitlement_id: str,
        payload: ReasonRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 撤销允许撤销的限时权益并记录原因.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     payload: 暂停或撤销权益的原因说明.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及活动版本,状态,启动截止,激活及到期时间,补救次数和版本号.
        return _serialize(
            await service.revoke(
                entitlement_id,
                str(admin.admin_user_id),
                idempotency_key,
                payload.reason,
                clock(),
            )
        )

    return router
