"""Human-reviewed lexicon and ASR candidates bound to an immutable draft dependency."""

import hashlib
import json
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.integrations.asr.aliyun import AliyunASR
from juya_admin_api.integrations.content_ai.aliyun import (
    AliyunContentAI,
    TextCandidate,
    input_token_upper_bound,
)
from juya_admin_api.modules.cloud_usage.store import CloudUsageStore, fence_within
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore, decode
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content_import.stages import Claim, StageStore
from juya_admin_api.modules.media.service import MediaService
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def dependency(content: SceneContent, version: int, kind: str, audio_sha: str = "") -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "content": content.model_dump(mode="json"),
                "edit_version": version,
                "kind": kind,
                "audio_sha": audio_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def missing_sources(content: SceneContent) -> dict[str, str]:
    covered = {
        source
        for entry in [*content.vocabulary, *content.chunks]
        for source in entry.source_sentence_ids
    }
    return {
        row.id: row.english
        for row in content.dialogue
        if row.english.strip() and row.id not in covered
    }


def lexicon_results(
    rows: tuple[TextCandidate, ...], sources: dict[str, str]
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        source = sources.get(row.source_id)
        if source is None or not row.english or row.english not in source:
            raise AppError("CANDIDATE_SOURCE_INVALID", "候选不属于原句", 409)
        start = source.find(row.english)
        unique = source.find(row.english, start + 1) == -1
        result.append(
            {
                "source_id": row.source_id,
                "english": row.english,
                "chinese": row.chinese,
                "start": start if unique else None,
                "end": start + len(row.english) if unique else None,
                "requires_manual_correction": not unique,
            }
        )
    return result


class CandidateService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        production: ProductionStore,
        stages: StageStore,
        *,
        ai: AliyunContentAI | None = None,
        asr: AliyunASR | None = None,
        media: MediaService | None = None,
        config_version: str = "v1",
        content_ai_daily_token_limit: int = 0,
        content_ai_max_output_tokens: int = 4096,
        asr_daily_seconds_limit: int = 0,
        max_asr_polls: int = 60,
        usage: CloudUsageStore | None = None,
    ) -> None:
        self.sessions, self.production, self.stages = sessions, production, stages
        self.ai, self.asr, self.media, self.config_version = ai, asr, media, config_version
        self.content_ai_daily_token_limit = content_ai_daily_token_limit
        self.content_ai_max_output_tokens = content_ai_max_output_tokens
        self.asr_daily_seconds_limit = asr_daily_seconds_limit
        self.max_asr_polls = max_asr_polls
        self.usage = usage or CloudUsageStore()
        if not 1 <= content_ai_max_output_tokens <= 32768 or not 1 <= max_asr_polls <= 120:
            raise ValueError("candidate output/poll limits invalid")

    async def _draft(
        self, session: AsyncSession, revision_id: str
    ) -> tuple[SceneRevision, SceneContent]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT r.public_id,r.edit_version,r.status,r.content_snapshot,"
                        "s.public_id AS scene_id FROM scene_revision r JOIN scene s ON "
                        "s.id=r.scene_id AND s.draft_revision_id=r.id WHERE "
                        "r.public_id=:id FOR UPDATE"
                    ),
                    {"id": revision_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "DRAFT":
            raise AppError("CANDIDATE_DRAFT_REQUIRED", "仅当前场景草稿支持候选", 409)
        content = SceneContent.model_validate(decode(row["content_snapshot"]))
        if content.template_type not in {"dialogue", "word_card", "vocabulary"}:
            raise AppError("CANDIDATE_TYPE_INVALID", "纯阅读短文不支持候选", 422)
        if not content.dialogue or any(
            not line.english.strip() or not line.chinese.strip() for line in content.dialogue
        ):
            raise AppError("CANDIDATE_TEXT_REQUIRED", "请先保存完整中英文正文", 409)
        return SceneRevision(
            row["public_id"],
            row["scene_id"],
            None,
            version=row["edit_version"],
            content=content.model_dump(mode="json"),
        ), content

    async def _audio(
        self, session: AsyncSession, content: SceneContent, verify_bytes: bool = True
    ) -> dict[str, Any]:
        if content.audio is None or self.media is None:
            raise AppError("ASR_AUDIO_REQUIRED", "请先保存确认整段音频", 409)
        audio = content.audio
        valid = await session.scalar(
            text(
                "SELECT v.id FROM audio_version v JOIN audio_target t ON "
                "t.id=v.target_id JOIN media_asset m ON m.id=v.asset_id WHERE "
                "v.public_id=:version AND t.public_id=:target AND "
                "m.public_id=:asset AND v.status IN ('ACTIVE','CONFIRMED','SUPERSEDED') "
                "AND m.status='CONFIRMED' AND m.security_status='PASSED'"
            ),
            {"version": audio.version_id, "target": audio.target_id, "asset": audio.asset_id},
        )
        if valid is None:
            raise AppError("ASR_AUDIO_INVALID", "整段音频固定版本无效", 409)
        asset = await self.media.get_asset(audio.asset_id)
        if not asset.duration_ms or asset.duration_ms <= 0:
            raise AppError("ASR_DURATION_REQUIRED", "整段音频缺少实际解码时长", 409)
        data = await self.media.read_asset_bytes(asset) if verify_bytes else None
        if data is not None and hashlib.sha256(data).hexdigest() != asset.sha256:
            raise AppError("ASR_AUDIO_HASH_INVALID", "整段音频字节已变化", 409)
        return {
            "asset_id": asset.id,
            "object_key": asset.object_key,
            "sha256": asset.sha256,
            "version_id": audio.version_id,
            "duration_ms": asset.duration_ms,
        }

    async def queue(
        self, revision_id: str, kind: str, expected_version: int, actor: str, now: datetime
    ) -> str:
        if kind not in {"AI_LEXICON", "ASR_BOUNDARY"}:
            raise AppError("CANDIDATE_KIND_INVALID", "候选类型无效", 422)
        if (kind == "AI_LEXICON" and self.ai is None) or (
            kind == "ASR_BOUNDARY" and self.asr is None
        ):
            raise AppError("CANDIDATE_PROVIDER_UNAVAILABLE", "候选服务未配置", 503)
        if (kind == "AI_LEXICON" and self.content_ai_daily_token_limit <= 0) or (
            kind == "ASR_BOUNDARY" and self.asr_daily_seconds_limit <= 0
        ):
            raise AppError("CLOUD_BUDGET_DISABLED", "请先设置云调用硬预算", 409)
        async with self.sessions() as session, session.begin():
            revision, content = await self._draft(session, revision_id)
            if expected_version != revision.version:
                raise AppError("REVISION_VERSION_CONFLICT", "草稿版本已变化", 409)
            sources = (
                missing_sources(content)
                if kind == "AI_LEXICON"
                else {line.id: line.english for line in content.dialogue}
            )
            if not sources:
                raise AppError("CANDIDATE_NOT_NEEDED", "当前正文无需补充候选", 409)
            audio = await self._audio(session, content, False) if kind == "ASR_BOUNDARY" else {}
            hashed = dependency(content, revision.version, kind, audio.get("sha256", ""))
            identifier = await self.stages.ensure_within(
                session, revision.id, kind, hashed, self.config_version
            )
            payload = {
                "kind": kind,
                "stage_id": identifier,
                "config_version": self.config_version,
                "edit_version": revision.version,
                "sources": sources,
                "audio": audio,
                "actor": actor,
            }
            await session.execute(
                text(
                    "INSERT INTO lexicon_candidate(public_id,content_type,content_id,"
                    "revision_id,input_hash,candidate,status) VALUES(:id,'SCENE',"
                    ":content,:revision,:hash,:payload,'PENDING') ON DUPLICATE KEY "
                    "UPDATE id=id"
                ),
                {
                    "id": identifier,
                    "content": revision.scene_id,
                    "revision": revision.id,
                    "hash": hashed,
                    "payload": json.dumps(payload),
                },
            )
        await self.stages.enqueue(identifier, now)
        return identifier

    async def get(self, identifier: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM lexicon_candidate WHERE public_id=:id"),
                        {"id": identifier},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("CANDIDATE_NOT_FOUND", "候选不存在", 404)
            payload = decode(row["candidate"])
            stage_id = payload.get("poll_stage_id") or payload.get("stage_id") or identifier
            stage = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,stage,status,config_version "
                            "FROM content_import_stage "
                            "WHERE public_id=:id"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            return {
                "id": identifier,
                "revision_id": row["revision_id"],
                "kind": payload.get("kind"),
                "stage_id": stage["public_id"] if stage else None,
                "stage_kind": stage["stage"] if stage else None,
                "stage_status": stage["status"] if stage else None,
                "config_hash": stage["config_version"] if stage else None,
                "status": row["status"],
                "input_hash": row["input_hash"],
                "candidate": payload,
            }

    async def retry(
        self,
        stage_id: str,
        expected_fence: int,
        input_hash: str,
        config_version: str,
        expected_edit_version: int,
        actor: str,
        key: str,
        acknowledged_charged_retry: bool = False,
    ) -> dict[str, Any]:
        if not actor or not key or len(key) > 128:
            raise AppError("CANDIDATE_RETRY_KEY_INVALID", "重试须提供管理员与幂等键", 422)
        request_hash = hashlib.sha256(
            json.dumps(
                [
                    stage_id,
                    expected_fence,
                    input_hash,
                    config_version,
                    expected_edit_version,
                    actor,
                    key,
                    acknowledged_charged_retry,
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        now = datetime.now(UTC).replace(tzinfo=None)
        scope = "v15.candidate.retry:" + stage_id
        async with self.sessions() as session, session.begin():
            stage = (
                (
                    await session.execute(
                        text("SELECT * FROM content_import_stage WHERE public_id=:id FOR UPDATE"),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            if stage is None or stage["stage"] not in {"AI_LEXICON", "ASR_BOUNDARY", "ASR_POLL"}:
                raise AppError("CANDIDATE_STAGE_NOT_FOUND", "候选阶段不存在", 404)
            # Lock the common stage before a missing idempotency row to avoid cross-gap deadlocks.
            replay = await self.production.creation_replay(session, scope, actor, key, request_hash)
            if replay:
                return {**replay, "replayed": True}
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM lexicon_candidate WHERE public_id=:id OR "
                            "JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.poll_stage_id'))=:id FOR UPDATE"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("CANDIDATE_NOT_FOUND", "候选不存在", 404)
            payload = decode(row["candidate"])
            replay_key = hashlib.sha256((actor + ":" + key).encode()).hexdigest()
            if payload.get("retry_key") == replay_key:
                if payload.get("retry_request_hash") != request_hash:
                    raise AppError("CANDIDATE_RETRY_KEY_CONFLICT", "幂等键已用于其他重试请求", 409)
                return {
                    "stage_id": stage_id,
                    "candidate_id": row["public_id"],
                    "status": stage["status"],
                    "replayed": True,
                }
            polling = stage["stage"] == "ASR_POLL"
            if not polling and row["status"] in {"UNKNOWN", "SUBMITTING", "SUBMITTED"}:
                raise AppError("PROVIDER_OUTCOME_UNKNOWN", "付费结果未明确,禁止重试提交", 409)
            if (
                stage["status"] != "FAILED"
                or stage["fencing_version"] != expected_fence
                or stage["input_hash"] != input_hash
                or stage["config_version"] != config_version
                or config_version != self.config_version
            ):
                raise AppError("CANDIDATE_RETRY_CONFLICT", "阶段状态或依赖已变化", 409)
            if row["status"] in {"ADOPTED", "REJECTED", "READY"}:
                raise AppError("CANDIDATE_RETRY_CONFLICT", "候选已完成或已人工处理", 409)
            revision, content = await self._draft(session, str(row["revision_id"]))
            audio = (
                await self._audio(session, content, False)
                if payload["kind"] == "ASR_BOUNDARY"
                else {}
            )
            if (
                revision.version != expected_edit_version
                or dependency(content, revision.version, payload["kind"], audio.get("sha256", ""))
                != input_hash
            ):
                raise AppError("CANDIDATE_INPUT_CHANGED", "当前草稿依赖已变化", 409)
            if polling:
                if (
                    not payload.get("provider_task_id")
                    or not payload.get("usage_attempt_id")
                    or int(payload.get("poll_count", 0)) >= self.max_asr_polls
                ):
                    raise AppError("ASR_POLL_RETRY_UNSAFE", "无固定云任务或轮询额度已耗尽", 409)
            else:
                prior = (
                    (
                        await session.execute(
                            text("SELECT status FROM content_cloud_attempt WHERE stage_id=:id"),
                            {"id": stage_id},
                        )
                    )
                    .mappings()
                    .all()
                )
                if any(item["status"] != "FAILED" for item in prior):
                    raise AppError("PROVIDER_OUTCOME_UNKNOWN", "历史调用尚未明确失败,禁止重试", 409)
                if prior and acknowledged_charged_retry is not True:
                    raise AppError("CHARGED_RETRY_ACK_REQUIRED", "再次收费须人工明确确认", 409)
                if (payload["kind"] == "AI_LEXICON" and self.ai is None) or (
                    payload["kind"] == "ASR_BOUNDARY" and self.asr is None
                ):
                    raise AppError("CANDIDATE_PROVIDER_UNAVAILABLE", "候选服务未配置", 503)
                retry_owner = new_ulid(datetime.now(UTC))
                next_fence = int(stage["fencing_version"]) + 1
                await session.execute(
                    text(
                        "UPDATE content_import_stage SET status='RUNNING',"
                        "fencing_version=:fence,lease_owner=:owner,lease_until=:until "
                        "WHERE public_id=:id"
                    ),
                    {
                        "id": stage_id,
                        "fence": next_fence,
                        "owner": retry_owner,
                        "until": now + timedelta(seconds=180),
                    },
                )
                claim = Claim(stage_id, next_fence, input_hash, config_version, retry_owner)
                units = (
                    input_token_upper_bound(payload["sources"]) + self.content_ai_max_output_tokens
                    if payload["kind"] == "AI_LEXICON"
                    else ((int(audio.get("duration_ms") or 0) + 999) // 1000) * 1000
                )
                limit = (
                    self.content_ai_daily_token_limit
                    if payload["kind"] == "AI_LEXICON"
                    else self.asr_daily_seconds_limit * 1000
                )
                payload["reserved_retry_attempt_id"] = await self.usage.reserve_within(
                    session,
                    claim,
                    "AI_TOKENS" if payload["kind"] == "AI_LEXICON" else "ASR_MILLISECONDS",
                    units,
                    limit,
                    authorized_failed_retry=True,
                    metadata={
                        "input_hash": input_hash,
                        "config_version": config_version,
                        "retry_actor": actor,
                        "provider": "aliyun",
                        "model": self.ai.model
                        if payload["kind"] == "AI_LEXICON" and self.ai
                        else self.asr.model
                        if self.asr
                        else "",
                        "measured_audio_duration_ms": audio.get("duration_ms"),
                    },
                )
                for field in (
                    "usage_attempt_id",
                    "provider_task_id",
                    "poll_stage_id",
                    "poll_count",
                    "results",
                    "_blocked_unknown",
                ):
                    payload.pop(field, None)
            payload.update(retry_key=replay_key, retry_request_hash=request_hash)
            await session.execute(
                text(
                    "UPDATE lexicon_candidate SET status=:status,candidate=:payload "
                    "WHERE public_id=:id"
                ),
                {
                    "id": row["public_id"],
                    "status": "SUBMITTED" if polling else "PENDING",
                    "payload": json.dumps(payload),
                },
            )
            await session.execute(
                text(
                    "UPDATE content_import_stage SET status='QUEUED',"
                    "fencing_version=fencing_version+1,lease_owner=NULL,lease_until=:until,"
                    "next_retry_at=NULL,error_code=NULL,output=NULL WHERE public_id=:id"
                ),
                {"id": stage_id, "until": now + timedelta(seconds=120)},
            )
            await session.execute(
                text(
                    "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,"
                    "payload,status,next_attempt_at,created_at) VALUES(:event,"
                    "'CONTENT_IMPORT_STAGE',:id,:payload,'PENDING',:now,:now)"
                ),
                {
                    "event": new_ulid(datetime.now(UTC)),
                    "id": stage_id,
                    "payload": json.dumps({"stage_id": stage_id}),
                    "now": now,
                },
            )
            result = {
                "stage_id": stage_id,
                "candidate_id": row["public_id"],
                "status": "QUEUED",
                "paid_retry": not polling,
                "replayed": False,
            }
            await self.production.remember_creation(
                session, scope, actor, key, request_hash, result, datetime.now(UTC)
            )
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,actor_public_id,action,object_type,"
                    "object_public_id,after_summary,request_id) "
                    "VALUES(:id,:actor,'candidate.retry','content_import_stage',:stage,:summary,:key)"
                ),
                {
                    "id": new_ulid(datetime.now(UTC)),
                    "actor": actor,
                    "stage": stage_id,
                    "summary": json.dumps(
                        {
                            **result,
                            "acknowledged_charged_retry": acknowledged_charged_retry,
                            "input_hash": input_hash,
                            "config_version": config_version,
                        }
                    ),
                    "key": key,
                },
            )
            return result

    async def _save(
        self,
        identifier: str,
        payload: dict[str, Any],
        status: str,
        claim: Claim,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            await fence_within(session, claim)
            if outcome is not None:
                await self.usage.record_within(
                    session, claim, payload["usage_attempt_id"], **outcome
                )
            await session.execute(
                text(
                    "UPDATE lexicon_candidate SET candidate=:payload,status=:status "
                    "WHERE public_id=:id AND status NOT IN ('ADOPTED','REJECTED')"
                ),
                {"id": identifier, "payload": json.dumps(payload), "status": status},
            )

            if status == "READY":
                await session.execute(
                    text(
                        "UPDATE content_import_stage SET status='SUCCEEDED',"
                        "output=:output,lease_owner=NULL,lease_until=NULL WHERE public_id=:id"
                    ),
                    {"id": claim.id, "output": json.dumps({"candidate_id": identifier})},
                )

    async def _initialize(
        self, stage_id: str, claim: Claim
    ) -> tuple[str, dict[str, Any], SceneRevision, dict[str, Any], bool]:
        async with self.sessions() as session, session.begin():
            await fence_within(session, claim)
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM lexicon_candidate WHERE public_id=:id OR "
                            "JSON_UNQUOTE(JSON_EXTRACT(candidate,'$.poll_stage_id'))=:id FOR UPDATE"
                        ),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("CANDIDATE_MISSING", "候选不存在", 404)
            identifier = str(row["public_id"])
            payload = decode(row["candidate"])
            revision, content = await self._draft(session, str(row["revision_id"]))
            audio = await self._audio(session, content) if payload["kind"] == "ASR_BOUNDARY" else {}
            if (
                dependency(content, revision.version, payload["kind"], audio.get("sha256", ""))
                != claim.input_hash
                or claim.config_version != self.config_version
            ):
                raise AppError("CANDIDATE_INPUT_CHANGED", "候选依赖已变化", 409)
            polling = payload.get("poll_stage_id") == stage_id
            if not polling:
                if row["status"] in {"SUBMITTING", "SUBMITTED", "UNKNOWN"}:
                    payload["_blocked_unknown"] = True
                    return identifier, payload, revision, audio, polling
                if payload["kind"] == "AI_LEXICON":
                    units = (
                        input_token_upper_bound(payload["sources"])
                        + self.content_ai_max_output_tokens
                    )
                    limit = self.content_ai_daily_token_limit
                    kind = "AI_TOKENS"
                else:
                    units = ((int(audio.get("duration_ms") or 0) + 999) // 1000) * 1000
                    limit = self.asr_daily_seconds_limit * 1000
                    kind = "ASR_MILLISECONDS"
                if payload.get("reserved_retry_attempt_id"):
                    if limit <= 0:
                        raise AppError("CLOUD_BUDGET_DISABLED", "云调用预算已关闭", 409)
                    attempt_id = str(payload.pop("reserved_retry_attempt_id"))
                    await self.usage.use_reserved_within(session, claim, attempt_id)
                    payload["usage_attempt_id"] = attempt_id
                else:
                    payload["usage_attempt_id"] = await self.usage.reserve_within(
                        session,
                        claim,
                        kind,
                        units,
                        limit,
                        metadata={
                            "provider": "aliyun",
                            "model": self.ai.model
                            if kind == "AI_TOKENS" and self.ai is not None
                            else self.asr.model
                            if self.asr is not None
                            else "",
                            "config_version": self.config_version,
                            "measured_audio_duration_ms": audio.get("duration_ms"),
                        },
                    )
                await session.execute(
                    text(
                        "UPDATE lexicon_candidate SET status='SUBMITTING',candidate=:payload "
                        "WHERE public_id=:id"
                    ),
                    {"id": identifier, "payload": json.dumps(payload)},
                )
            return identifier, payload, revision, audio, polling

    async def run(self, stage_id: str, owner: str, now: datetime) -> dict[str, Any] | None:
        claim = await self.stages.claim(stage_id, owner, now)
        if claim is None:
            return None
        try:
            identifier, payload, revision, audio, polling = await self._initialize(stage_id, claim)
        except AppError as exc:
            with suppress(AppError):
                await self.stages.fail(claim, exc.code, datetime.now(UTC), False)
            raise
        if payload.pop("_blocked_unknown", False):
            outcome: dict[str, Any] | None = (
                {"status": "UNKNOWN"} if "usage_attempt_id" in payload else None
            )
            await self._save(identifier, payload, "UNKNOWN", claim, outcome)
            await self.stages.fail(claim, "PROVIDER_OUTCOME_UNKNOWN", datetime.now(UTC), False)
            raise AppError("PROVIDER_OUTCOME_UNKNOWN", "调用结果未知,请人工核对", 409)
        # Providers execute outside transactions. Unknown submissions require manual checks.
        try:
            if payload["kind"] == "AI_LEXICON":
                if self.ai is None:
                    raise AppError("CANDIDATE_PROVIDER_UNAVAILABLE", "AI服务未配置", 503)
                response = await self.ai.suggest(
                    payload["sources"], max_output_tokens=self.content_ai_max_output_tokens
                )
                payload["results"] = lexicon_results(response.candidates, payload["sources"])
                payload["usage"] = response.usage
                payload["provider_request_id"] = response.request_id
                await self._save(
                    identifier,
                    payload,
                    "READY",
                    claim,
                    {
                        "status": "SUCCEEDED",
                        "actual_units": response.usage["total_tokens"],
                        "usage": response.usage,
                        "request_id": response.request_id,
                    },
                )
            elif not polling:
                if self.asr is None or self.media is None:
                    raise AppError("CANDIDATE_PROVIDER_UNAVAILABLE", "ASR服务未配置", 503)
                signed = await self.media.sign_media(audio["object_key"], None, now)
                payload["provider_task_id"] = await self.asr.submit(signed.url)
                async with self.sessions() as session, session.begin():
                    await fence_within(session, claim)
                    poll_id = await self.stages.ensure_within(
                        session, revision.id, "ASR_POLL", claim.input_hash, claim.config_version
                    )
                    payload["poll_stage_id"] = poll_id
                    await session.execute(
                        text(
                            "UPDATE lexicon_candidate SET candidate=:payload,"
                            "status='SUBMITTED' WHERE public_id=:id"
                        ),
                        {"id": identifier, "payload": json.dumps(payload)},
                    )
                    await self.usage.record_within(
                        session,
                        claim,
                        payload["usage_attempt_id"],
                        "SUBMITTED",
                        request_id=payload["provider_task_id"],
                    )
                await self.stages.enqueue(poll_id, now)
            else:
                if self.asr is None:
                    raise AppError("CANDIDATE_PROVIDER_UNAVAILABLE", "ASR服务未配置", 503)
                payload["poll_count"] = int(payload.get("poll_count", 0)) + 1
                if payload["poll_count"] > self.max_asr_polls:
                    raise AppError("ASR_POLL_LIMIT", "ASR查询次数已达上限,请人工核对任务", 409)
                await self._save(identifier, payload, "SUBMITTED", claim)
                provider_response = await self.asr.poll(payload["provider_task_id"])
                output = provider_response.get("output", {})
                if output.get("task_status") in {"PENDING", "RUNNING"}:
                    if payload["poll_count"] >= self.max_asr_polls:
                        raise AppError("ASR_POLL_LIMIT", "ASR查询次数已达上限,请人工核对任务", 409)
                    await self.stages.fail(claim, "ASR_PENDING", now, True)
                    return {"candidate_id": identifier, "pending": True}
                results = output.get("results", [output["result"]] if "result" in output else [])
                if output.get("task_status") != "SUCCEEDED" or len(results) != 1:
                    raise AppError("ASR_RESULT_INVALID", "ASR识别结果异常", 409)
                candidate = await self.asr.parse_result(results[0]["transcription_url"])
                provider_usage = provider_response.get("usage", {})
                seconds = (
                    provider_usage.get("seconds") if isinstance(provider_usage, dict) else None
                )
                actual_units = seconds * 1000 if type(seconds) is int and seconds >= 0 else None
                payload["usage"] = {
                    "measured_audio_duration_ms": audio.get("duration_ms"),
                    "provider_usage": provider_usage,
                    "provider_request_id": provider_response.get("request_id"),
                }
                payload["results"] = {
                    "text": candidate.text,
                    "boundaries": [
                        {"start_ms": part.start_ms, "end_ms": part.end_ms}
                        for part in candidate.boundaries
                    ],
                    "requires_manual_alignment": True,
                    "timing_confirmed": False,
                }
                await self._save(
                    identifier,
                    payload,
                    "READY",
                    claim,
                    {
                        "status": "SUCCEEDED",
                        "actual_units": actual_units,
                        "usage": payload["usage"],
                        "request_id": str(
                            provider_response.get("request_id") or payload["provider_task_id"]
                        ),
                    },
                )
            result: dict[str, object] = {"candidate_id": identifier}
            if payload["kind"] == "ASR_BOUNDARY" and not polling:
                await self.stages.finish(claim, result, datetime.now(UTC))
            return result
        except Exception as exc:
            # A stale worker must never overwrite a newer owner's candidate or billing row.
            if isinstance(exc, AppError) and exc.code == "IMPORT_STAGE_LEASE_LOST":
                raise
            permanent_poll = isinstance(exc, AppError) and exc.code == "ASR_RESULT_INVALID"
            retry_poll = (
                polling
                and not permanent_poll
                and int(payload.get("poll_count", 0)) < self.max_asr_polls
            )
            outcome = (
                {
                    "status": "FAILED" if permanent_poll else "UNKNOWN",
                    "request_id": payload.get("provider_task_id"),
                }
                if "usage_attempt_id" in payload
                else None
            )
            with suppress(AppError):
                await self._save(
                    identifier,
                    payload,
                    "SUBMITTED" if retry_poll else ("FAILED" if permanent_poll else "UNKNOWN"),
                    claim,
                    outcome,
                )
            with suppress(AppError):
                await self.stages.fail(
                    claim, "CANDIDATE_PROVIDER_FAILED", datetime.now(UTC), retry_poll
                )
            raise

    async def adopt(
        self,
        identifier: str,
        index: int,
        source_id: str,
        start: int,
        end: int,
        chinese: str,
        expected_version: int,
        actor: str,
        now: datetime,
        entry_type: str = "VOCABULARY",
    ) -> SceneRevision:
        if entry_type not in {"VOCABULARY", "PHRASE"}:
            raise AppError("CANDIDATE_ENTRY_TYPE_INVALID", "词条类型无效", 422)
        async with self.sessions() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM lexicon_candidate WHERE public_id=:id FOR UPDATE"),
                        {"id": identifier},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] != "READY":
                raise AppError("CANDIDATE_NOT_READY", "候选未就绪", 409)
            payload = decode(row["candidate"])
            if payload["kind"] != "AI_LEXICON":
                raise AppError("ASR_MANUAL_BOUNDARY_REQUIRED", "ASR只供人工边界核对", 409)
            revision, content = await self._draft(session, str(row["revision_id"]))
            if (
                revision.version != expected_version
                or dependency(content, revision.version, "AI_LEXICON") != row["input_hash"]
                or payload["config_version"] != self.config_version
            ):
                raise AppError("CANDIDATE_INPUT_CHANGED", "草稿依赖已变化", 409)
            results = payload.get("results", [])
            if not 0 <= index < len(results):
                raise AppError("CANDIDATE_INDEX_INVALID", "候选条目不存在", 422)
            selected = results[index]
            sentence = next((line for line in content.dialogue if line.id == source_id), None)
            if (
                sentence is None
                or selected["source_id"] != source_id
                or not 0 <= start < end <= len(sentence.english)
                or sentence.english[start:end] != selected["english"]
                or not chinese.strip()
            ):
                raise AppError("CANDIDATE_SOURCE_INVALID", "请准确校正来源位置和中文", 422)
            content.schema_version = 2
            entry = SceneEntry(
                english=selected["english"],
                chinese=chinese,
                source_sentence_ids=[source_id],
                source_sentence=sentence.english,
                source_locator={"sentence_id": source_id, "start": str(start), "end": str(end)},
                confirmed=False,
            )
            (content.vocabulary if entry_type == "VOCABULARY" else content.chunks).append(entry)
            saved = await self.production.save_revision_within(
                session,
                replace(revision, content=content.model_dump(mode="json"), created_by=actor),
                expected_version,
            )
            await session.execute(
                text(
                    "UPDATE lexicon_candidate SET status='ADOPTED',decided_by=:actor,"
                    "decided_at=:now WHERE public_id=:id"
                ),
                {"id": identifier, "actor": actor, "now": now.astimezone(UTC).replace(tzinfo=None)},
            )
            return saved
