"""Administrators edit module metadata, while readiness remains server-owned."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.learning_features.modules import LearningModulesStore


class ModuleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=80)
    sort_order: int = Field(ge=0, le=65535)
    enabled: bool


def create_module_admin_router(
    store: LearningModulesStore,
    *,
    current_admin: Callable[..., Awaitable[SessionRecord]],
    current_admin_write: Callable[..., Awaitable[SessionRecord]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content/learning-modules", tags=["learning-modules"])

    @router.get("")
    async def listing(admin: Annotated[SessionRecord, Depends(current_admin)]) -> dict[str, Any]:
        return await store.listing()

    @router.put("/{module_type}")
    async def update(
        module_type: str,
        payload: ModuleCommand,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(module_type, str(admin.admin_user_id), key, payload.model_dump())

    return router
