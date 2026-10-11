from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal["dialogue", "word_card", "short_reading"]
    content_id: str
    revision_id: str
    candidate_hash: str = Field(pattern="^[0-9a-f]{64}$")


class CreateProofreadingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_id: str | None = None
    candidates: list[CandidateRequest] = Field(max_length=10000)


class AppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_ids: list[str] = Field(min_length=1, max_length=10000)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_hash: str = Field(pattern="^[0-9a-f]{64}$")
    text_checked: bool
    images_checked: bool = False
    audio_listened: bool = False


class FailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error_code: str = Field(min_length=1, max_length=128)


def create_proofreading_router(
    store: ProofreadingStore,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content/proofreading", tags=["admin-proofreading"])

    @router.post("")
    async def create(
        body: CreateProofreadingRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, Any]:
        return await store.create(
            [item.model_dump(exclude_none=True) for item in body.candidates],
            str(admin.admin_user_id),
            set_id=body.set_id,
        )

    @router.get("/{set_id}")
    async def get(
        set_id: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await store.get(set_id)

    @router.post("/{set_id}/samples")
    async def append(
        set_id: str,
        body: AppendRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, bool]:
        await store.append_samples(set_id, body.revision_ids, str(admin.admin_user_id))
        return {"ok": True}

    @router.post("/{set_id}/items/{revision_id}/confirm")
    async def confirm(
        set_id: str,
        revision_id: str,
        body: ConfirmRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, bool]:
        await store.confirm(
            set_id,
            revision_id,
            body.candidate_hash,
            str(admin.admin_user_id),
            body.model_dump(exclude={"candidate_hash"}),
        )
        return {"ok": True}

    @router.post("/{set_id}/items/{revision_id}/fail")
    async def fail(
        set_id: str,
        revision_id: str,
        body: FailRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, bool]:
        await store.fail(set_id, revision_id, body.error_code, str(admin.admin_user_id))
        return {"ok": True}

    @router.post("/{set_id}/complete")
    async def complete(
        set_id: str, admin: Annotated[SessionRecord, Depends(current_admin_write)]
    ) -> dict[str, bool]:
        await store.complete(set_id, str(admin.admin_user_id))
        return {"ok": True}

    return router
