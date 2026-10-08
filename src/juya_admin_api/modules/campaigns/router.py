"""Administrator campaign query and command routes."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.campaigns.service import CampaignService

AdminDependency = Callable[..., Awaitable[SessionRecord]]
CampaignStatus = Literal["DRAFT", "OPEN", "PAUSED", "ENDED", "ARCHIVED", "CLOSED"]
CampaignOperation = Literal["open", "pause", "resume", "end", "archive", "capacity"]


class CampaignVersionResponse(BaseModel):
    id: str
    version_no: int
    status: str
    duration_days: int
    activation_window_days: int
    capacity: int
    granted_user_count: int
    grant_starts_at: datetime | None
    grant_ends_at: datetime | None
    locked_at: datetime | None
    version: int
    scene_ids: list[str]


class CampaignResponse(BaseModel):
    id: str
    name: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    current_version: CampaignVersionResponse | None
    available_operations: list[CampaignOperation | Literal["copy"]]


class CampaignListItemResponse(BaseModel):
    id: str
    name: str
    status: str
    version: int
    current_version_id: str | None = None
    capacity: int | None = None
    granted_user_count: int | None = None
    created_at: datetime
    updated_at: datetime
    available_operations: list[CampaignOperation | Literal["copy"]]


class CampaignPageResponse(BaseModel):
    items: list[CampaignListItemResponse]
    page: int
    page_size: int
    total: int


class CampaignSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=200)
    duration_days: Literal[3, 5] | None = None
    activation_window_days: int | None = Field(default=None, ge=1)
    capacity: int | None = Field(default=None, ge=0)
    scene_ids: list[str] | None = None


class CampaignCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    capacity: int | None = Field(default=None, ge=0)


# 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
# 参数: 无.
# 返回: 当前带 UTC 时区的日期时间.
def create_campaign_router(
    service: CampaignService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    # 功能: 创建活动列表,保存,版本复制和状态操作路由.
    # 参数:
    #     service: 执行业务操作的限时活动服务.
    #     current_admin: 注入已认证管理员会话的只读依赖.
    #     current_admin_write: 同时校验管理员身份和 CSRF 的写操作依赖.
    #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/api/v1/admin/campaigns", tags=["campaigns"])
    key_header = Header(alias="X-Idempotency-Key", min_length=1, max_length=100)

    @router.get("", response_model=CampaignPageResponse)
    async def list_campaigns(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        status: Annotated[CampaignStatus | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        # 功能: 按状态分页查询活动及版本.
        # 参数:
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        #     status: 活动,反馈或权益的业务状态筛选条件.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        # 返回: items 活动列表及 page,page_size,total 分页字段;各项包含当前版本及可用操作.
        return await service.list({} if status is None else {"status": status}, page, page_size)

    @router.get("/{campaign_id}", response_model=CampaignResponse)
    async def get_campaign(
        campaign_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        # 功能: 获取活动及当前版本详情.
        # 参数:
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     _admin: 认证依赖注入的管理员会话,仅用于执行访问校验.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        return await service.get(campaign_id)

    @router.post("", response_model=CampaignResponse, status_code=201)
    async def create_campaign(
        payload: CampaignSaveRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, key_header],
    ) -> dict[str, object]:
        # 功能: 创建活动和初始版本并保证请求幂等.
        # 参数:
        #     payload: 活动名称,期限,启动窗口,容量,场景及预期版本.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        return await service.save(
            None,
            actor_id=str(admin.admin_user_id),
            idempotency_key=idempotency_key,
            now=clock(),
            **_save_fields(payload),
        )

    @router.put("/{campaign_id}", response_model=CampaignResponse)
    async def update_campaign(
        campaign_id: str,
        payload: CampaignSaveRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, key_header],
    ) -> dict[str, object]:
        # 功能: 按预期版本保存活动和可编辑版本字段.
        # 参数:
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     payload: 活动名称,期限,启动窗口,容量,场景及预期版本.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        return await service.save(
            campaign_id,
            actor_id=str(admin.admin_user_id),
            idempotency_key=idempotency_key,
            now=clock(),
            **_save_fields(payload),
        )

    @router.post("/{campaign_id}/versions/copy", response_model=CampaignResponse)
    async def copy_version(
        campaign_id: str,
        payload: CampaignCommandRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, key_header],
    ) -> dict[str, object]:
        # 功能: 复制活动版本作为可编辑的新版本.
        # 参数:
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     payload: 活动操作的预期版本及可选新容量.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        return await service.command(
            campaign_id,
            "copy",
            expected_version=payload.expected_version,
            actor_id=str(admin.admin_user_id),
            idempotency_key=idempotency_key,
            now=clock(),
        )

    @router.post("/{campaign_id}/commands/{operation}", response_model=CampaignResponse)
    async def command_campaign(
        campaign_id: str,
        operation: CampaignOperation,
        payload: CampaignCommandRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, key_header],
    ) -> dict[str, object]:
        # 功能: 执行活动发布,关闭,容量调整等状态命令.
        # 参数:
        #     campaign_id: 限时活动公开标识;创建活动时可为 None.
        #     operation: 待执行的业务命令,例如授予,暂停,恢复或撤销.
        #     payload: 活动操作的预期版本及可选新容量.
        #     admin: 经认证且按接口要求完成 CSRF 校验的管理员会话.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 活动标识,名称,状态,版本和时间,以及含期限,容量,场景的 current_version 和可用操作.
        #     重试返回已保存幂等响应.
        return await service.command(
            campaign_id,
            operation,
            expected_version=payload.expected_version,
            capacity=payload.capacity,
            actor_id=str(admin.admin_user_id),
            idempotency_key=idempotency_key,
            now=clock(),
        )

    return router


def _save_fields(payload: CampaignSaveRequest) -> dict[str, object]:
    # 功能: 提取活动保存请求中已经提交的业务字段.
    # 参数:
    #     payload: 活动名称,期限,启动窗口,容量,场景及预期版本.
    # 返回: expected_version,name 及非空期限,启动窗口,容量,场景字段;场景标识转为元组.
    fields: dict[str, object] = {"expected_version": payload.expected_version, "name": payload.name}
    for key in ("duration_days", "activation_window_days", "capacity"):
        value = getattr(payload, key)
        if value is not None:
            fields[key] = value
    if payload.scene_ids is not None:
        fields["scene_ids"] = tuple(payload.scene_ids)
    return fields
