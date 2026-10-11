"""Explicit, dependency checked recovery of failed local and charged candidate stages."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content.v15_materials import LegacyMaterialService
from juya_admin_api.modules.content_import.parser import canonical_hash
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class StageAdminStore:
    def __init__(
        self,
        imports: ImportService,
        authoring: SceneAuthoringStore,
        materials: LegacyMaterialService,
        candidates: CandidateService,
    ):
        self.imports, self.authoring = imports, authoring
        self.materials, self.candidates = materials, candidates

    async def get(self, stage_id: str) -> dict[str, Any]:
        async with self.imports.sessions() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,item_id,stage,input_hash,config_version,"
                            "status,attempt,fencing_version,lease_until,next_retry_at,error_code "
                            "FROM content_import_stage "
                            "WHERE public_id=:id"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("IMPORT_STAGE_NOT_FOUND", "处理阶段不存在", 404)
            return dict(row)

    async def retry(
        self, stage_id: str, actor: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        row = await self.get(stage_id)
        if payload.get("manual_confirmed") is not True:
            raise AppError("STAGE_RETRY_CONFIRMATION_REQUIRED", "需要明确确认重试此阶段", 422)
        if (
            row["stage"] in {"AI_LEXICON", "ASR_BOUNDARY", "ASR_POLL", "SENTENCE_SLICE"}
            and payload.get("expected_edit_version") is None
        ):
            raise AppError("STAGE_RETRY_EDIT_VERSION_REQUIRED", "需要当前草稿编辑版本", 422)
        if str(row["stage"]).startswith("PUBLICATION_RANGE_"):
            raise AppError(
                "STAGE_RETRY_USE_PUBLICATION_RANGE", "请明确选择冻结发布范围的失败项重试", 409
            )
        if row["stage"] in {"AI_LEXICON", "ASR_BOUNDARY", "ASR_POLL"}:
            return await self.candidates.retry(
                stage_id,
                payload["expected_fence"],
                payload["input_hash"],
                payload["config_version"],
                payload["expected_edit_version"],
                actor,
                key,
                payload.get("acknowledged_charged_retry", False),
            )
        request_hash = canonical_hash(payload)
        scope = "v15.stage.retry:" + stage_id
        deferred = None
        async with self.imports.sessions() as session, session.begin():
            replay = await self.imports.production.creation_replay(
                session, scope, actor, key, request_hash
            )
            if replay:
                return replay
            batch = None
            if row["stage"] != "SENTENCE_SLICE":
                batch = await self.imports.batch(session, row["item_id"], True)
                if batch["status"] == "CANCELLED":
                    raise AppError("IMPORT_CANCELLED", "已取消批次不能重试", 409)
            original = await session.scalar(
                text(
                    "SELECT payload FROM admin_outbox WHERE JSON_UNQUOTE("
                    "JSON_EXTRACT(payload,'$.stage_id'))=:id AND event_type IN "
                    "('CONTENT_SENTENCE_SLICE','CONTENT_IMPORT_CONFIRM') ORDER BY id DESC LIMIT 1"
                ),
                {"id": stage_id},
            )
            command = json.loads(original) if isinstance(original, str) else (original or {})
            if row["stage"] == "SENTENCE_SLICE":
                if not command.get("revision_id") or not command.get("sentence_id"):
                    raise AppError("STAGE_REQUEST_MISSING", "原切片请求已缺失", 409)
                revision, content = await self.authoring.locked_revision(
                    session, command["revision_id"], payload["expected_edit_version"]
                )
                current = self.authoring.make_input(
                    revision,
                    content,
                    command["sentence_id"],
                    await self.authoring.source(session, content),
                )
                if current.dependency(self.authoring.encoding_config) != row["input_hash"]:
                    raise AppError("SLICE_DEPENDENCY_CHANGED", "切片当前依赖已变化", 409)
            locked = (
                (
                    await session.execute(
                        text("SELECT * FROM content_import_stage WHERE public_id=:id FOR UPDATE"),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .one()
            )
            if (
                locked["status"] != "FAILED"
                or locked["fencing_version"] != payload["expected_fence"]
                or locked["input_hash"] != payload["input_hash"]
                or locked["config_version"] != payload["config_version"]
            ):
                raise AppError("STAGE_RETRY_STALE", "阶段状态或依赖已变化,请刷新", 409)
            if row["stage"] == "CONFIRM":
                assert batch is not None
                if not isinstance(command.get("confirmation"), dict):
                    raise AppError("STAGE_REQUEST_MISSING", "原导入确认请求缺失", 409)
                deferred = ("confirm", row["item_id"], command["confirmation"])
            elif str(row["stage"]).startswith("MATERIAL_BIND_"):
                assert batch is not None
                manifest = (
                    json.loads(batch["manifest"])
                    if isinstance(batch["manifest"], str)
                    else batch["manifest"]
                )
                binding = manifest.get("binding_requests", {}).get(stage_id)
                if binding is None:
                    raise AppError("MATERIAL_REQUEST_MISSING", "原绑定请求缺失", 409)
                deferred = ("materials", row["item_id"], binding["request"])
            elif row["stage"] in {"VALIDATE", "MATERIAL_VALIDATE", "SENTENCE_SLICE"}:
                if batch is not None and row["input_hash"] != batch["input_hash"]:
                    raise AppError("IMPORT_ARTIFACT_CHANGED", "批次输入已变化", 409)
                if batch is not None:
                    await self.imports.original(batch)
                    await session.execute(
                        text(
                            "UPDATE content_import_batch SET status='PENDING' WHERE public_id=:id"
                        ),
                        {"id": row["item_id"]},
                    )
                now = datetime.now(UTC)
                await session.execute(
                    text(
                        "UPDATE content_import_stage SET status='QUEUED',"
                        "fencing_version=fencing_version+1,lease_owner=NULL,lease_until=:until,"
                        "next_retry_at=NULL,error_code=NULL,output=NULL "
                        "WHERE public_id=:id"
                    ),
                    {"id": stage_id, "until": now + timedelta(seconds=120)},
                )
                await session.execute(
                    text(
                        "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,payload) "
                        "VALUES(:event,'CONTENT_IMPORT_STAGE',:id,:payload)"
                    ),
                    {
                        "event": new_ulid(now),
                        "id": stage_id,
                        "payload": json.dumps({"stage_id": stage_id}),
                    },
                )
                result = {
                    "stage_id": stage_id,
                    "status": "QUEUED",
                    "fencing_version": locked["fencing_version"] + 1,
                }
                await self.imports.production.remember_creation(
                    session, scope, actor, key, request_hash, result, now
                )
                await self.authoring.proofreading.audit(
                    session, stage_id, actor, "stage.retry", result
                )
                return result
            else:
                raise AppError("STAGE_RETRY_UNSUPPORTED", "请使用对应范围发布的失败项恢复命令", 409)
        # These commands mint a fresh execution identity; old per-item receipts remain intact.
        assert deferred is not None
        if deferred[0] == "confirm":
            return await self.imports.queue_confirmation(deferred[1], deferred[2], actor, key)
        body = deferred[2]
        return await self.materials.queue_bind(
            deferred[1],
            body["revision_id"],
            body["expected_version"],
            True,
            body["bindings"],
            actor,
            key,
        )
