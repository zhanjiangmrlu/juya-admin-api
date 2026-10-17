"""Explicit administrator publication-range preview and queued confirmation."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.publication_ranges import PublicationRangeService
from juya_admin_api.modules.content.router import AdminDependency


class PublicationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal["dialogue", "word_card", "short_reading"]
    content_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    expected_edit_version: int | None = Field(default=None, ge=1)
    candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    warning_codes: list[str] = Field(default_factory=list, max_length=100)


class PreviewRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[PublicationCandidate] = Field(min_length=1, max_length=1000)


class ConfirmRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manual_confirmed: bool
    expected_version: int = Field(ge=1)
    collection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_revision_ids: list[str] = Field(min_length=1, max_length=1000)


def create_publication_range_router(
    service: PublicationRangeService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/content/publication-ranges", tags=["publication-ranges"]
    )

    @router.post("")
    async def preview(
        payload: PreviewRange,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await service.preview(
            [candidate.model_dump(exclude_none=True) for candidate in payload.candidates],
            str(admin.admin_user_id),
        )

    @router.get("/{token}")
    async def recover(
        token: str, admin: Annotated[SessionRecord, Depends(current_admin)], response: Response
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await service.recover(token, str(admin.admin_user_id))

    @router.post("/{token}/confirm", status_code=202)
    async def confirm(
        token: str,
        payload: ConfirmRange,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
        response: Response,
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return await service.confirm(token, str(admin.admin_user_id), key, **payload.model_dump())

    return router
