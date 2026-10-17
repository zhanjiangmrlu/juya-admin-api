"""Durable automatic audio preparation triggered by saving the actual scene input."""

import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.cloud_usage.store import fence_within
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def audio_input_hash(content: SceneContent) -> str:
    # content 提供固定音频绑定及原句顺序,翻译和词条编辑不重复启动识别
    return snapshot_hash(
        {
            "audio": content.audio.model_dump(mode="json") if content.audio else None,
            "sentences": [{"id": row.id, "english": row.english} for row in content.dialogue],
        }
    )


def needs_alignment(content: SceneContent) -> bool:
    # content 已有完整且有序的验证边界时直接保留,缺失边界才进入自动生产
    audio = content.audio
    if (
        content.schema_version != 2
        or content.template_type != "dialogue"
        or not audio
        or not content.dialogue
    ):
        return False
    previous_end = 0
    for row in content.dialogue:
        if (
            not row.english.strip()
            or row.start_ms is None
            or row.end_ms is None
            or not previous_end <= row.start_ms < row.end_ms <= audio.duration_ms
            or not row.timing_confirmed
            or not row.boundary_verified_by
            or row.audio_version_id != audio.version_id
        ):
            return True
        previous_end = row.end_ms
    return False


async def schedule_audio_within(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    revision_id: str,
    content: SceneContent,
) -> str | None:
    # session 与保存草稿同事务,sessions 提供stage仓储,revision_id/content 固定自动任务输入
    if (
        content.schema_version != 2
        or content.template_type != "dialogue"
        or not content.audio
        or not content.dialogue
    ):
        return None
    stage_id = await StageStore(sessions).ensure_within(
        session,
        revision_id,
        "AUTO_AUDIO",
        audio_input_hash(content),
        "auto-audio-v1",
    )
    await session.execute(
        text(
            "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,payload) "
            "VALUES(:event,'CONTENT_IMPORT_STAGE',:revision,:payload)"
        ),
        {
            "event": new_ulid(datetime.now(UTC)),
            "revision": revision_id,
            "payload": json.dumps({"stage_id": stage_id}),
        },
    )
    return stage_id


class AutoAudioService:
    def __init__(
        self, candidates: CandidateService, authoring: SceneAuthoringStore | None = None
    ) -> None:
        # candidates 复用已有硬预算、幂等云任务及租约控制的候选服务
        self.candidates = candidates
        self.sessions = candidates.sessions
        self.stages = candidates.stages
        self.authoring = authoring

    async def run(self, stage_id: str) -> None:
        # stage_id 标识本地自动调度阶段,重放仅复用已有候选,不重复提交供应商任务
        claim = await self.stages.claim(stage_id, uuid4().hex, datetime.now(UTC))
        if claim is None:
            return
        try:
            async with self.sessions() as session, session.begin():
                await fence_within(session, claim)
                stage = (
                    await session.execute(
                        text("SELECT item_id FROM content_import_stage WHERE public_id=:id"),
                        {"id": stage_id},
                    )
                ).one()
                revision, content = await self.candidates._draft(session, stage.item_id)
                if audio_input_hash(content) != claim.input_hash:
                    raise AppError("AUTO_AUDIO_INPUT_CHANGED", "自动音频输入已变化", 409)
                needed = needs_alignment(content)
                actor = await session.scalar(
                    text("SELECT created_by FROM scene_revision WHERE public_id=:id"),
                    {"id": revision.id},
                )
                if not needed:
                    if self.authoring is None:
                        raise AppError(
                            "AUTO_AUDIO_AUTHORING_UNAVAILABLE", "自动切片服务未配置", 503
                        )
                    await self.authoring.prepare_verified_slices_within(session, revision.id)
            candidate_id = (
                await self.candidates.queue(
                    revision.id,
                    "ASR_BOUNDARY",
                    revision.version,
                    str(actor or "system:auto-audio"),
                    datetime.now(UTC),
                )
                if needed
                else None
            )
            await self.stages.finish(
                claim,
                {"candidate_id": candidate_id, "status": "QUEUED" if needed else "ALREADY_ALIGNED"},
                datetime.now(UTC),
            )
        except Exception as error:
            code = error.code if isinstance(error, AppError) else "AUTO_AUDIO_FAILED"
            await self.stages.fail(claim, code, datetime.now(UTC), False)

    async def state(self, revision_id: str) -> dict[str, Any]:
        # revision_id 只读取当前草稿的匹配输入阶段,不发起收费请求
        async with self.sessions() as session, session.begin():
            _, content = await self.candidates._draft(session, revision_id)
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id AS stage_id,status,error_code,input_hash,output "
                            "FROM content_import_stage WHERE item_id=:revision "
                            "AND stage='AUTO_AUDIO' "
                            "AND input_hash=:hash ORDER BY id DESC LIMIT 1"
                        ),
                        {"revision": revision_id, "hash": audio_input_hash(content)},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("AUTO_AUDIO_NOT_FOUND", "当前草稿尚无自动音频任务", 404)
            result = dict(row)
            output = result.pop("output") or {}
            if isinstance(output, str):
                output = json.loads(output)
            result["candidate_id"] = output.get("candidate_id")
            return result


def create_auto_audio_router(
    service: AutoAudioService, current_admin: AdminDependency
) -> APIRouter:
    # service 提供真实阶段状态,current_admin 要求现有管理员读权限
    router = APIRouter(prefix="/api/v1/admin/content", tags=["content-audio-production"])

    @router.get("/revisions/{revision_id}/audio-production")
    async def state(
        revision_id: str, _admin: Annotated[Any, Depends(current_admin)]
    ) -> dict[str, Any]:
        # revision_id 定位当前保存草稿,_admin 是已验证管理员会话
        return await service.state(revision_id)

    return router
