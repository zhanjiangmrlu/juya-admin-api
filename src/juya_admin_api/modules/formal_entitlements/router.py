from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.formal_entitlements.domain import (
    EntitlementOperation,
    EntitlementTerm,
    FormalEntitlement,
    FormalEntitlementCommand,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.modules.user_projection.service import UserProjectionService
from juya_admin_api.shared.errors import AppError


class EntitlementCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)
    package_id: str = Field(min_length=1, max_length=64)
    term: EntitlementTerm | None = None
    reason: str | None = Field(default=None, max_length=500)


AdminDependency = Callable[..., Awaitable[SessionRecord]]
EntitlementType = Literal["FORMAL", "LIMITED"]
EntitlementStatus = Literal[
    "ACTIVE", "PAUSED", "REVOKED", "PENDING", "ENDED", "START_EXPIRED", "EXPIRED"
]


class EntitlementQueryRepository(Protocol):
    async def list_entitlements(
        self, filters: dict[str, str], page: int, page_size: int
    ) -> dict[str, Any]:
        # 功能: 按用户,类型,状态和到期筛选分页查询权益.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     filters: 业务列表的筛选条件映射,空映射表示不限.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 正式及限时权益列表和 page,page_size,total 分页字段.
        ...

    async def list_packages(self, page: int, page_size: int) -> dict[str, Any]:
        # 功能: 分页查询可用于正式权益授予的课程套餐.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 启用套餐列表和 page,page_size,total;每项含 id,name,status,sort_order.
        ...

    async def get_formal(self, entitlement_id: str) -> dict[str, Any] | None:
        # 功能: 查询正式权益及关联用户,套餐详情.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        # 返回: 正式权益及套餐名称,包含期限,到期时间,版本号和可用操作.无匹配权益时为 None.
        ...

    async def get_limited(self, entitlement_id: str) -> dict[str, Any] | None:
        # 功能: 查询限时权益及关联活动和学习统计.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     entitlement_id: 待查询或变更的权益标识.
        # 返回: 限时权益及关联活动信息,固定场景标识和可用操作.无匹配权益时为 None.
        ...


class EntitlementListItemResponse(BaseModel):
    id: str
    type: EntitlementType
    user_id: str
    status: str
    granted_at: datetime
    expires_at: datetime | None
    package_id: str | None
    campaign_id: str | None

    juya_number: str = ""
    nickname: str | None = None
    wechat_id: str | None = None
    contact_status: str = "NOT_PROVIDED"
    contact_degraded: bool = False
    content_name: str = ""
    campaign_version_id: str | None = None
    campaign_version_no: int | None = None
    term: str | None = None
    effective_at: datetime | None = None
    start_deadline: datetime | None = None


class EntitlementPageResponse(BaseModel):
    items: list[EntitlementListItemResponse]
    page: int
    page_size: int
    total: int


class PackageResponse(BaseModel):
    id: str
    name: str
    status: str
    sort_order: int


class PackagePageResponse(BaseModel):
    items: list[PackageResponse]
    page: int
    page_size: int
    total: int


class FormalEntitlementDetailResponse(BaseModel):
    id: str
    user_id: str
    package_id: str
    package_name: str
    status: str
    term: str
    granted_at: datetime
    expires_at: datetime | None
    version: int
    available_operations: list[str]


def create_entitlement_query_router(
    repository: EntitlementQueryRepository,
    *,
    current_admin: AdminDependency,
    users: UserProjectionService | None = None,
) -> APIRouter:
    # 功能: 创建正式和限时权益的统一查询路由.
    # 参数:
    #     repository: 提供正式权益持久化和查询能力的仓储.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     users: 用户搜索,详情和联系人投影服务.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin", tags=["entitlements"])

    @router.get("/entitlements", response_model=EntitlementPageResponse)
    async def list_entitlements(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        response: Response,
        user_id: str | None = None,
        type: Annotated[EntitlementType | None, Query()] = None,
        status: Annotated[EntitlementStatus | None, Query()] = None,
        package_id: str | None = None,
        campaign_id: str | None = None,
        campaign_version_id: str | None = None,
        expiry: Literal["EXPIRING", "ENDING", "START_EXPIRING"] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        # 功能: 按用户,类型,状态和到期筛选分页查询权益.
        # 参数:
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        #     response: 当前 HTTP 响应,用于设置 Cookie,禁止缓存等头部.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     type: 权益类型筛选条件.
        #     status: 活动,反馈或权益的业务状态筛选条件.
        #     package_id: 正式权益关联的课程套餐公开标识.
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     campaign_version_id: 限时活动版本公开标识,权益绑定该版本的固定场景集合.
        #     expiry: 权益临近到期或启动窗口到期的筛选模式.
        #     date_from: 按日期筛选的起始边界;None 表示不限制.
        #     date_to: 按日期筛选的结束边界;None 表示不限制.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 正式及限时权益列表和 page,page_size,total 分页字段.
        #     配置用户服务时各项补充微信号及联系信息降级标记.
        filters = {
            key: value
            for key, value in {
                "user_id": user_id,
                "type": type,
                "status": status,
                "package_id": package_id,
                "campaign_id": campaign_id,
                "campaign_version_id": campaign_version_id,
                "expiry": expiry,
                "date_from": date_from,
                "date_to": date_to,
            }.items()
            if value is not None
        }
        response.headers["Cache-Control"] = "no-store"
        result = await repository.list_entitlements(filters, page, page_size)
        if users is not None and result["items"]:
            contacts, degraded = await users.contacts_for(
                tuple(dict.fromkeys(item["user_id"] for item in result["items"])),
                admin_id=str(_admin.admin_user_id),
                occurred_at=datetime.now(UTC),
            )
            for item in result["items"]:
                contact = contacts.get(item["user_id"])
                item["wechat_id"] = None if contact is None else contact.wechat_id
                item["contact_degraded"] = degraded
        return result

    @router.get("/content-packages", response_model=PackagePageResponse)
    async def list_packages(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        # 功能: 分页查询可用于正式权益授予的课程套餐.
        # 参数:
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 启用套餐列表和 page,page_size,total;每项含 id,name,status,sort_order.
        return await repository.list_packages(page, page_size)

    return router


def _serialize(entitlement: FormalEntitlement) -> dict[str, object]:
    # 功能: 将正式权益领域对象转换为接口响应字段.
    # 参数:
    #     entitlement: 当前权益领域对象,提供状态,期限和业务关联信息.
    # 返回: 权益标识,用户及套餐标识,状态,期限,开通和到期时间,版本号.
    return {
        "id": entitlement.id,
        "user_id": entitlement.user_id,
        "package_id": entitlement.package_id,
        "status": entitlement.status,
        "term": entitlement.term,
        "granted_at": entitlement.granted_at,
        "expires_at": entitlement.expires_at,
        "version": entitlement.version,
    }


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_formal_entitlement_router(
    service: FormalEntitlementService,
    *,
    query_repository: EntitlementQueryRepository,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建正式权益详情,操作预览及执行路由.
    # 参数:
    #     service: 执行业务操作的正式权益服务.
    #     query_repository: 查询正式和限时权益明细及套餐列表的仓储.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     current_admin_write: 同时校验管理员身份和 CSRF 的写操作依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(
        prefix="/api/v1/admin/formal-entitlements",
        tags=["formal-entitlements"],
    )

    @router.get("/{entitlement_id}", response_model=FormalEntitlementDetailResponse)
    async def get_entitlement(
        entitlement_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        # 功能: 查询指定权益详情,不存在时返回业务错误.
        # 参数:
        #     entitlement_id: 待查询或变更的权益标识.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 正式权益及套餐名称,包含期限,到期时间,版本号和可用操作.不存在时抛出业务错误.
        result = await query_repository.get_formal(entitlement_id)
        if result is None:
            raise AppError("FORMAL_ENTITLEMENT_NOT_FOUND", "正式权益不存在", 404)
        return result

    @router.post("/preview-operation")
    async def preview_operation(
        payload: EntitlementCommandRequest,
        operation: EntitlementOperation,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能: 计算权益操作结果供管理员预览,不持久化变更.
        # 参数:
        #     payload: 用户,套餐,期限及权益操作原因.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 权益标识,用户及套餐标识,状态,期限,开通和到期时间,版本号.
        command = FormalEntitlementCommand(
            payload.user_id,
            payload.package_id,
            operation,
            payload.term,
            payload.reason,
        )
        return _serialize(await service.preview_operation(command, clock()))

    async def apply(
        operation: EntitlementOperation,
        payload: EntitlementCommandRequest,
        admin: SessionRecord,
        idempotency_key: str,
    ) -> dict[str, object]:
        # 功能: 执行正式权益命令并保存权益,幂等结果及审计.
        # 参数:
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     payload: 用户,套餐,期限及权益操作原因.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及套餐标识,状态,期限,开通和到期时间,版本号.
        command = FormalEntitlementCommand(
            payload.user_id,
            payload.package_id,
            operation,
            payload.term,
            payload.reason,
        )
        result = await service.apply_operation(
            command,
            str(admin.admin_user_id),
            idempotency_key,
            clock(),
        )
        return _serialize(result)

    @router.post("/commands/{operation}")
    async def apply_operation(
        operation: EntitlementOperation,
        payload: EntitlementCommandRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, Header(alias="X-Idempotency-Key")],
    ) -> dict[str, object]:
        # 功能: 执行权益命令并持久化幂等结果和审计记录.
        # 参数:
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     payload: 用户,套餐,期限及权益操作原因.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 权益标识,用户及套餐标识,状态,期限,开通和到期时间,版本号.
        return await apply(operation, payload, admin, idempotency_key)

    return router
