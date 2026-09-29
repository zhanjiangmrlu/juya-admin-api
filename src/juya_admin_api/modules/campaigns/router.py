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


def create_campaign_router(
    service: CampaignService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/campaigns", tags=["campaigns"])
    key_header = Header(alias="X-Idempotency-Key", min_length=1, max_length=100)

    @router.get("", response_model=CampaignPageResponse)
    async def list_campaigns(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        status: Annotated[CampaignStatus | None, Query()] = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        return await service.list({} if status is None else {"status": status}, page, page_size)

    @router.get("/{campaign_id}", response_model=CampaignResponse)
    async def get_campaign(
        campaign_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, object]:
        return await service.get(campaign_id)

    @router.post("", response_model=CampaignResponse, status_code=201)
    async def create_campaign(
        payload: CampaignSaveRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        idempotency_key: Annotated[str, key_header],
    ) -> dict[str, object]:
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
    fields: dict[str, object] = {"expected_version": payload.expected_version, "name": payload.name}
    for key in ("duration_days", "activation_window_days", "capacity"):
        value = getattr(payload, key)
        if value is not None:
            fields[key] = value
    if payload.scene_ids is not None:
        fields["scene_ids"] = tuple(payload.scene_ids)
    return fields
