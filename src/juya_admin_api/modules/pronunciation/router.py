from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from juya_admin_api.modules.pronunciation.service import PronunciationService


class PronunciationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str
    user_id: str
    content_type: str | None = None
    content_id: str | None = None
    content_version: str | None = None
    entry_id: str | None = None
    favorite_id: str | None = None
    snapshot_version: str | None = None
    snapshot_revision_id: str | None = None
    target_id: str | None = None
    resource_version: str | None = None
    nonce: str | None = None


def create_router(
    service: PronunciationService, trusted_bff_dependency: Callable[..., Any]
) -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1/pronunciations", dependencies=[Depends(trusted_bff_dependency)]
    )

    @router.post("/{target_id}/resolve")
    async def resolve(target_id: str, context: PronunciationContext) -> dict[str, Any]:
        return await service.resolve(target_id, context.model_dump(exclude_none=True))

    @router.post("/tasks/{task_id}")
    async def task(task_id: str, context: PronunciationContext) -> dict[str, Any]:
        return await service.poll(task_id, context.model_dump(exclude_none=True))

    return router
