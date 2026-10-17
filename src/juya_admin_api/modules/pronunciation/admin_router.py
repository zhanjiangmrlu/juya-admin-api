from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.pronunciation.admin_store import PronunciationAdminStore


class FixedPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_version: str = Field(min_length=1, max_length=64)


def create_pronunciation_admin_router(
    store: PronunciationAdminStore,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/pronunciations", tags=["pronunciations"])

    @router.get("/targets/{target_id}")
    async def get(
        target_id: str, response: Response, _admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await store.get(target_id)

    @router.post("/targets/{target_id}/preview")
    async def preview(
        target_id: str,
        payload: FixedPreviewRequest,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await store.preview(target_id, payload.resource_version)

    @router.post("/tasks/{task_id}/retry", status_code=202)
    async def retry(
        task_id: str,
        response: Response,
        _admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await store.retry(task_id)

    return router
