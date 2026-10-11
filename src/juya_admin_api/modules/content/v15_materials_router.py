from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content.v15_materials import LegacyMaterialService
from juya_admin_api.shared.errors import AppError


class MaterialBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=240)
    role: Literal[
        "COVER_IMAGE",
        "LEARNING_IMAGE",
        "WORD_IMAGE",
        "WHOLE_AUDIO",
        "FULL_AUDIO",
        "WORD_AUDIO",
        "CHUNK_AUDIO",
    ]
    entry_id: str | None = Field(default=None, min_length=1, max_length=64)


class MaterialBindingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    manual_checked: bool
    bindings: list[MaterialBinding] = Field(min_length=1, max_length=1000)


def create_materials_router(
    service: LegacyMaterialService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content-materials", tags=["admin-content-materials"])

    @router.post("", status_code=202)
    async def upload(
        file: Annotated[UploadFile, File()],
        series_id: Annotated[str, Form(min_length=1, max_length=64)],
        content_type: Annotated[Literal["dialogue", "word_card", "short_reading"], Form()],
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > service.imports.limits.max_package_bytes:
                raise AppError("MATERIAL_PACKAGE_LIMIT", "素材包超过限制", 413)
        return await service.create(
            series_id, content_type, bytes(data), str(admin.admin_user_id), key
        )

    @router.get("/{batch_id}")
    async def preview(
        batch_id: str, _admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await service.preview(batch_id)

    @router.post("/{batch_id}/bindings", status_code=202)
    async def bind(
        batch_id: str,
        payload: MaterialBindingsRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        return await service.queue_bind(
            batch_id,
            payload.revision_id,
            payload.expected_version,
            payload.manual_checked,
            [binding.model_dump(exclude_none=True) for binding in payload.bindings],
            str(admin.admin_user_id),
            key,
        )

    @router.post("/{batch_id}/cancel")
    async def cancel(
        batch_id: str, admin: Annotated[SessionRecord, Depends(current_admin_write)]
    ) -> dict[str, Any]:
        return await service.cancel(batch_id, str(admin.admin_user_id))

    return router
