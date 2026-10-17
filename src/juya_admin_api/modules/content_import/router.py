"""Upload quickly, then inspect persisted previews and enqueue confirmation."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.shared.errors import AppError


class ConfirmImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(min_length=64, max_length=64)
    preview_version: int = Field(ge=1)
    selected: list[str] = Field(min_length=1, max_length=1000)
    acknowledged_defaults: list[str] = Field(default_factory=list, max_length=2000)
    confirmed_conflicts: list[str] = Field(default_factory=list, max_length=3000)


def create_import_router(
    service: ImportService, *, current_admin: AdminDependency, current_admin_write: AdminDependency
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content-imports", tags=["admin-content-imports"])

    @router.post("", status_code=202)
    async def upload(
        file: Annotated[UploadFile, File()],
        series_id: Annotated[str, Form(min_length=1, max_length=64)],
        content_type: Annotated[Literal["dialogue", "word_card", "short_reading"], Form()],
        mode: Annotated[Literal["ZIP", "DOCX"], Form()],
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > service.limits.max_package_bytes:
                raise AppError("IMPORT_PACKAGE_TOO_LARGE", "文件超过导入上限", 413)
        return await service.create(
            series_id=series_id,
            content_type=content_type,
            mode=mode,
            data=bytes(data),
            actor=str(admin.admin_user_id),
            key=key,
        )

    @router.get("/{batch_id}")
    async def preview(
        batch_id: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await service.preview(batch_id)

    @router.post("/{batch_id}/confirm", status_code=202)
    async def confirm(
        batch_id: str,
        payload: ConfirmImport,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        return await service.queue_confirmation(
            batch_id, payload.model_dump(), str(admin.admin_user_id), key
        )

    @router.post("/{batch_id}/cancel")
    async def cancel(
        batch_id: str, admin: Annotated[SessionRecord, Depends(current_admin_write)]
    ) -> dict[str, Any]:
        return await service.cancel(batch_id, str(admin.admin_user_id))

    return router
