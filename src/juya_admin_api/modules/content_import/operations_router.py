"""Active limits, explicit stage recovery and authenticated archive previews."""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content_import.admin_stages import StageAdminStore
from juya_admin_api.modules.content_import.preview import ImportPreviewStore


class StageRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fence: int = Field(ge=0)
    input_hash: str = Field(pattern="^[0-9a-f]{64}$")
    config_version: str = Field(min_length=1, max_length=128)
    expected_edit_version: int | None = Field(default=None, ge=1)
    manual_confirmed: bool
    acknowledged_charged_retry: bool = False


class ResourcePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=240)


class BatchStage(BaseModel):
    stage_id: str
    item_id: str
    stage: str
    input_hash: str
    config_version: str
    status: str
    attempt: int
    fencing_version: int
    lease_until: datetime | None
    next_retry_at: datetime | None
    error_code: str | None


class BatchStagesResponse(BaseModel):
    batch_id: str
    stages: list[BatchStage]


def create_import_operations_router(
    stages: StageAdminStore,
    previews: ImportPreviewStore,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["content-import-operations"])

    @router.get("/content-imports/limits")
    async def limits(_admin: Annotated[SessionRecord, Depends(current_admin)]) -> dict[str, Any]:
        return {
            **asdict(stages.imports.limits),
            "content_types": ["dialogue", "word_card", "short_reading"],
            "modes": ["ZIP", "DOCX", "MATERIAL_ZIP"],
            "preview_ttl_seconds": 60,
        }

    @router.get("/content-imports/{batch_id}/stages", response_model=BatchStagesResponse)
    async def batch_stages(
        batch_id: str, _admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await stages.for_batch(batch_id)

    @router.get("/content-stages/{stage_id}")
    async def stage(
        stage_id: str, _admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await stages.get(stage_id)

    @router.post("/content-stages/{stage_id}/retry", status_code=202)
    async def retry(
        stage_id: str,
        body: StageRetryRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        return await stages.retry(stage_id, str(admin.admin_user_id), key, body.model_dump())

    @router.post("/content-imports/{batch_id}/resources/preview")
    async def preview(
        batch_id: str,
        body: ResourcePreviewRequest,
        response: Response,
        admin: Annotated[SessionRecord, Depends(current_admin)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await previews.issue(batch_id, body.path, str(admin.admin_user_id))

    @router.get("/content-imports/{batch_id}/resources/preview-content")
    async def preview_content(
        batch_id: str,
        token: Annotated[str, Query(min_length=1, max_length=4096)],
        admin: Annotated[SessionRecord, Depends(current_admin)],
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> Response:
        data, content_type = await previews.content(batch_id, token, str(admin.admin_user_id))
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
        }
        if range_header:
            try:
                unit, extent = range_header.split("=", 1)
                first, last = extent.split("-", 1)
                if unit != "bytes" or "," in extent:
                    raise ValueError("range")
                if first:
                    start, end = int(first), int(last) if last else len(data) - 1
                else:
                    suffix = int(last)
                    if suffix <= 0:
                        raise ValueError("suffix")
                    start, end = max(0, len(data) - suffix), len(data) - 1
                if not 0 <= start <= end or start >= len(data):
                    raise ValueError("extent")
                end = min(end, len(data) - 1)
            except ValueError:
                return Response(
                    status_code=416, headers=headers | {"Content-Range": f"bytes */{len(data)}"}
                )
            return Response(
                data[start : end + 1],
                status_code=206,
                media_type=content_type,
                headers=headers | {"Content-Range": f"bytes {start}-{end}/{len(data)}"},
            )
        return Response(data, media_type=content_type, headers=headers)

    return router
