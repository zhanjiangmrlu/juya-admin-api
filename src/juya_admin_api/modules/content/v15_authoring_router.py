from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore


class ManualEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    manual_checked: bool


class ManualBoundaryRequest(ManualEntryRequest):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class ManualSliceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    dependency_hash: str = Field(pattern="^[0-9a-f]{64}$")
    listened: bool


class EntryBatchCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str
    expected_version: int = Field(ge=1)
    candidate_hash: str = Field(pattern="^[0-9a-f]{64}$")


class EntryBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[EntryBatchCandidate] = Field(min_length=1, max_length=1000)
    manual_checked: bool


def create_authoring_router(
    store: SceneAuthoringStore,
    *,
    current_admin_write: AdminDependency,
    current_admin: AdminDependency | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content/revisions", tags=["admin-authoring"])
    read_admin = current_admin or current_admin_write

    @router.post("/entry-confirmations")
    async def entries(
        body: EntryBatchRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        return await store.confirm_entries_bulk(
            [item.model_dump() for item in body.candidates],
            str(admin.admin_user_id),
            body.manual_checked,
        )

    @router.get("/{revision_id}/sentences/{sentence_id}/slice-preview")
    async def preview(
        revision_id: str,
        sentence_id: str,
        dependency_hash: Annotated[str, Query(pattern="^[0-9a-f]{64}$")],
        admin: Annotated[SessionRecord, Depends(read_admin)],
    ) -> JSONResponse:
        result = await store.slice_preview(revision_id, sentence_id, dependency_hash)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/{revision_id}/entries/{entry_id}/confirm")
    async def entry(
        revision_id: str,
        entry_id: str,
        body: ManualEntryRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        return await store.confirm_entry(
            revision_id,
            entry_id,
            body.expected_version,
            str(admin.admin_user_id),
            body.manual_checked,
        )

    @router.post("/{revision_id}/sentences/{sentence_id}/boundary-confirm")
    async def boundary(
        revision_id: str,
        sentence_id: str,
        body: ManualBoundaryRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        return await store.confirm_boundary(
            revision_id,
            sentence_id,
            body.expected_version,
            body.start_ms,
            body.end_ms,
            str(admin.admin_user_id),
            body.manual_checked,
        )

    @router.post("/{revision_id}/sentences/{sentence_id}/slice-confirm")
    async def slice_confirm(
        revision_id: str,
        sentence_id: str,
        body: ManualSliceRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        return await store.confirm_slice(
            revision_id,
            sentence_id,
            body.expected_version,
            body.dependency_hash,
            str(admin.admin_user_id),
            body.listened,
        )

    return router
