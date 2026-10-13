from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import (
    AdminDependency,
    _serialize_revision,
)
from juya_admin_api.modules.content.v15_candidates import CandidateService


class QueueCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["AI_LEXICON", "ASR_BOUNDARY"]
    expected_version: int = Field(ge=1)


class AdoptCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    source_id: str = Field(min_length=1, max_length=64)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    chinese: str = Field(min_length=1, max_length=10000)
    expected_version: int = Field(ge=1)
    entry_type: Literal["VOCABULARY", "PHRASE"] = "VOCABULARY"


def create_candidate_router(
    service: CandidateService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content", tags=["content-candidates"])

    @router.post("/revisions/{revision_id}/candidates", status_code=202)
    async def queue(
        revision_id: str,
        payload: QueueCandidateRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, str]:
        identifier = await service.queue(
            revision_id,
            payload.kind,
            payload.expected_version,
            str(admin.admin_user_id),
            datetime.now(UTC),
        )
        return {"stage_id": identifier, "candidate_id": identifier, "status": "ACCEPTED"}

    @router.get("/candidates/{candidate_id}")
    async def get(
        candidate_id: str, _admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        return await service.get(candidate_id)

    @router.get("/revisions/{revision_id}/candidates/latest")
    async def get_latest(
        revision_id: str,
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        kind: Literal["AI_LEXICON", "ASR_BOUNDARY"] = "ASR_BOUNDARY",
    ) -> dict[str, Any]:
        # 功能:页面刷新时只读恢复指定草稿的最新持久候选和真实阶段状态。
        return await service.get_latest(revision_id, kind)

    @router.post("/candidates/{candidate_id}/adoptions")
    async def adopt(
        candidate_id: str,
        payload: AdoptCandidateRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
    ) -> dict[str, object]:
        return _serialize_revision(
            await service.adopt(
                candidate_id,
                payload.index,
                payload.source_id,
                payload.start,
                payload.end,
                payload.chinese,
                payload.expected_version,
                str(admin.admin_user_id),
                datetime.now(UTC),
                payload.entry_type,
            )
        )

    return router
