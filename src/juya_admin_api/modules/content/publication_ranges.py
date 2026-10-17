"""Frozen explicit publication ranges with durable per-item receipts and fenced workers."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.content_import.stages import Claim, StageStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

Candidate = dict[str, Any]
Inspect = Callable[[Candidate], Awaitable[Candidate]]
Publish = Callable[[Candidate, str, str], Awaitable[Candidate]]
CONFIG = "publication-range-v1"
PREFIX = "PUBLICATION_RANGE_"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalize(candidates: list[Candidate]) -> list[Candidate]:
    if not 1 <= len(candidates) <= 1000:
        raise AppError("PUBLICATION_RANGE_SIZE_INVALID", "发布范围须包含1至1000个候选", 422)
    revisions: set[str] = set()
    objects: set[tuple[str, str]] = set()
    result = []
    for item in candidates:
        required = {
            "content_type",
            "content_id",
            "revision_id",
            "expected_version",
            "candidate_hash",
        }
        if not required <= item.keys() or item["content_type"] not in {
            "dialogue",
            "word_card",
            "short_reading",
        }:
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "发布候选字段无效", 422)
        for key in ("content_id", "revision_id"):
            if not isinstance(item[key], str) or not 1 <= len(item[key]) <= 64:
                raise AppError("PUBLICATION_CANDIDATE_INVALID", "候选标识无效", 422)
        if type(item["expected_version"]) is not int or item["expected_version"] < 1:
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "候选版本无效", 422)
        if item["content_type"] == "short_reading" and (
            type(item.get("expected_edit_version")) is not int or item["expected_edit_version"] < 1
        ):
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "短文须提供草稿编辑版本", 422)
        if (
            not isinstance(item["candidate_hash"], str)
            or len(item["candidate_hash"]) != 64
            or any(ch not in "0123456789abcdef" for ch in item["candidate_hash"])
        ):
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "候选摘要无效", 422)
        identity = (item["content_type"], item["content_id"])
        if item["revision_id"] in revisions or identity in objects:
            raise AppError("PUBLICATION_CANDIDATE_DUPLICATE", "同一内容或候选不可重复", 422)
        revisions.add(item["revision_id"])
        objects.add(identity)
        normalized = {key: item[key] for key in sorted(required)}
        if item["content_type"] == "short_reading":
            normalized["expected_edit_version"] = item["expected_edit_version"]
        warnings = item.get("warning_codes", [])
        if not isinstance(warnings, list) or any(
            not isinstance(code, str) or not code or len(code) > 64 for code in warnings
        ):
            raise AppError("PUBLICATION_CANDIDATE_INVALID", "警告代码无效", 422)
        normalized["warning_codes"] = sorted(set(warnings))
        result.append(normalized)
    return sorted(result, key=lambda item: item["revision_id"])


def verify_facts(candidate: Candidate, facts: Candidate) -> None:
    if facts.get("ready") is not True:
        raise AppError("PUBLICATION_CANDIDATE_NOT_READY", "候选尚未满足发布条件", 409)
    for key, expected in candidate.items():
        actual = sorted(set(facts.get(key, []))) if key == "warning_codes" else facts.get(key)
        if actual != expected:
            raise AppError("PUBLICATION_PREVIEW_STALE", "候选版本或资料已变化, 请重新预览", 409)


class PublicationRangeService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        stages: StageStore,
        inspect_candidate: Inspect,
        publish_candidate: Publish,
    ):
        self.sessions, self.stages = sessions, stages
        self.inspect_candidate, self.publish_candidate = inspect_candidate, publish_candidate

    async def preview(
        self,
        candidates: list[Candidate],
        actor: str,
        now: datetime | None = None,
        *,
        token: str | None = None,
    ) -> Candidate:
        now = now or datetime.now(UTC)
        frozen = normalize(candidates)
        if token is not None:
            async with self.sessions() as session:
                exists = await session.scalar(
                    text("SELECT public_id FROM publication_range WHERE public_id=:id"),
                    {"id": token},
                )
            if exists:
                previous = await self.recover(token, actor)
                if previous["candidates"] != frozen:
                    raise AppError("PUBLICATION_PREVIEW_STALE", "已冻结发布候选不能替换", 409)
                return previous
        for candidate in frozen:
            verify_facts(candidate, await self.inspect_candidate(candidate))
        token = token or new_ulid(datetime.now(UTC))
        async with self.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO "
                    "publication_range(public_id,actor_id,collection_hash,candidates,r"
                    "esults,status,version,created_at,expires_at) VALUES(:id,:actor,:h"
                    "ash,:candidates,:results,'PREVIEW',1,:now,:expires)"
                ),
                {
                    "id": token,
                    "actor": actor,
                    "hash": digest(frozen),
                    "candidates": json.dumps(frozen),
                    "results": json.dumps({"items": {}, "commands": {}}),
                    "now": utc(now).replace(tzinfo=None),
                    "expires": utc(now + timedelta(minutes=15)).replace(tzinfo=None),
                },
            )
        return await self.recover(token, actor)

    async def _row(
        self, session: AsyncSession, token: str, actor: str | None, *, lock: bool = False
    ) -> Candidate:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,actor_id,collection_hash,status,version,stage_id,"
                        "created_at,"
                        "expires_at FROM publication_range WHERE public_id=:id"
                        + (" FOR UPDATE" if lock else " FOR SHARE")
                    ),
                    {"id": token},
                )
            )
            .mappings()
            .first()
        )
        if row is None or (actor is not None and row["actor_id"] != actor):
            raise AppError("PUBLICATION_RANGE_NOT_FOUND", "发布范围不存在", 404)
        result = dict(row)
        result["token"] = result["public_id"]
        for column in ("candidates", "results"):
            result[column] = await read_json(
                session, "publication_range", column, {"public_id": token}
            )
        return result

    async def recover(self, token: str, actor: str) -> Candidate:
        async with self.sessions() as session:
            return await self._row(session, token, actor)

    async def confirm(
        self,
        token: str,
        actor: str,
        key: str,
        *,
        manual_confirmed: bool,
        expected_version: int,
        collection_hash: str,
        selected_revision_ids: list[str],
        now: datetime | None = None,
    ) -> Candidate:
        now = now or datetime.now(UTC)
        selected = sorted(selected_revision_ids)
        if not selected or len(selected) != len(set(selected)) or len(selected) > 1000:
            raise AppError("PUBLICATION_SELECTION_INVALID", "请选择不重复的发布候选", 422)
        request_hash = digest(
            {
                "token": token,
                "actor": actor,
                "confirmed": manual_confirmed,
                "version": expected_version,
                "hash": collection_hash,
                "selected": selected,
            }
        )
        receipt_id = digest(key)
        async with self.sessions() as session, session.begin():
            row = await self._row(session, token, actor, lock=True)
            state = row["results"]
            receipt = state["commands"].get(receipt_id)
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
                return dict(receipt)
            if manual_confirmed is not True:
                raise AppError("PUBLICATION_CONFIRMATION_REQUIRED", "必须明确人工确认发布", 422)
            if utc(now) >= utc(row["expires_at"]):
                raise AppError("PUBLICATION_PREVIEW_EXPIRED", "发布预览已过期", 409)
            if expected_version != row["version"] or collection_hash != row["collection_hash"]:
                raise AppError("PUBLICATION_PREVIEW_STALE", "发布范围已变化, 请刷新", 409)
            if row["status"] == "QUEUED":
                raise AppError("PUBLICATION_RANGE_RUNNING", "已有发布范围正在执行", 409)
            by_revision = {candidate["revision_id"]: candidate for candidate in row["candidates"]}
            if not set(selected) <= by_revision.keys():
                raise AppError("PUBLICATION_SELECTION_INVALID", "选择超出已冻结发布范围", 422)
            pending = [
                revision
                for revision in selected
                if state["items"].get(revision, {}).get("status") != "SUCCEEDED"
            ]
            if not pending:
                raise AppError("PUBLICATION_SELECTION_COMPLETE", "所选候选已经发布成功", 409)
            for revision in pending:
                if state["items"].get(revision, {}).get("status") != "UNKNOWN":
                    verify_facts(
                        by_revision[revision], await self.inspect_candidate(by_revision[revision])
                    )
            stage = await self.stages.ensure_within(
                session, token, PREFIX + request_hash[:12], request_hash, CONFIG
            )
            receipt = {
                "request_hash": request_hash,
                "stage_id": stage,
                "token": token,
                "selected_revision_ids": selected,
            }
            state["commands"][receipt_id] = receipt
            await session.execute(
                text(
                    "UPDATE publication_range SET "
                    "results=:results,status='QUEUED',version=version+1,stage_id=:stage WHERE "
                    "public_id=:id"
                ),
                {"id": token, "results": json.dumps(state), "stage": stage},
            )
            # Stage and outbox commit in the same transaction as confirmation.
            await session.execute(
                text(
                    "UPDATE content_import_stage SET "
                    "status='QUEUED',fencing_version=fencing_version+1,lease_until=:until WHERE "
                    "public_id=:id"
                ),
                {"id": stage, "until": utc(now + timedelta(seconds=120)).replace(tzinfo=None)},
            )
            await session.execute(
                text(
                    "INSERT INTO "
                    "admin_outbox(event_id,event_type,aggregate_public_id,payload,stat"
                    "us,next_attempt_at,created_at) VALUES(:event,'CONTENT_IMPORT_STAG"
                    "E',:id,:payload,'PENDING',:now,:now)"
                ),
                {
                    "event": new_ulid(datetime.now(UTC)),
                    "id": stage,
                    "payload": json.dumps({"stage_id": stage}),
                    "now": utc(now).replace(tzinfo=None),
                },
            )
            return receipt

    async def _stage(self, session: AsyncSession, claim: Claim) -> str:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT "
                        "item_id,stage,status,fencing_version,lease_owner,lease_until,inpu"
                        "t_hash,config_version FROM content_import_stage WHERE public_id=:"
                        "id FOR UPDATE"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["status"] != "RUNNING"
            or row["fencing_version"] != claim.fencing_version
            or row["lease_owner"] != claim.owner
            or utc(row["lease_until"]) <= datetime.now(UTC)
            or row["input_hash"] != claim.input_hash
            or row["config_version"] != CONFIG
            or not row["stage"].startswith(PREFIX)
        ):
            raise AppError("IMPORT_STAGE_LEASE_LOST", "发布阶段租约已失效", 409)
        return str(row["item_id"])

    async def run(self, stage_id: str, owner: str, *, lease_seconds: int = 1800) -> Candidate:
        claim = await self.stages.claim(stage_id, owner, datetime.now(UTC), lease_seconds)
        if claim is None:
            return {"stage_id": stage_id, "claimed": False}
        try:
            async with self.sessions() as session, session.begin():
                token = await self._stage(session, claim)
                row = await self._row(session, token, None, lock=True)
                receipt = next(
                    (
                        value
                        for value in row["results"]["commands"].values()
                        if value["stage_id"] == stage_id
                    ),
                    None,
                )
                if row["stage_id"] != stage_id or receipt is None:
                    raise AppError("PUBLICATION_STAGE_STALE", "发布任务已被替换", 409)
                candidates = {
                    candidate["revision_id"]: candidate for candidate in row["candidates"]
                }
                selected = receipt["selected_revision_ids"]
            for revision in selected:
                # Stage lock is held across the publisher's separate transaction: cancellation or
                # reclamation waits. Per-item keys remain stable across crashes and new retry jobs.
                async with self.sessions() as session, session.begin():
                    await self._stage(session, claim)
                    row = await self._row(session, token, None, lock=True)
                    if row["stage_id"] != stage_id:
                        raise AppError("PUBLICATION_STAGE_STALE", "发布任务已被替换", 409)
                    state = row["results"]
                    if state["items"].get(revision, {}).get("status") == "SUCCEEDED":
                        continue
                    candidate = candidates[revision]
                    item_key = digest(["publication-range-item", token, candidate])
                    try:
                        result = await self.publish_candidate(candidate, row["actor_id"], item_key)
                        state["items"][revision] = {
                            "status": "SUCCEEDED",
                            "result": result,
                            "idempotency_key": item_key,
                        }
                    except AppError as error:
                        state["items"][revision] = {
                            "status": "FAILED",
                            "error_code": error.code,
                            "idempotency_key": item_key,
                        }
                    except Exception:
                        state["items"][revision] = {
                            "status": "UNKNOWN",
                            "error_code": "PUBLICATION_ITEM_RESULT_UNKNOWN",
                            "idempotency_key": item_key,
                        }
                    await self._stage(session, claim)
                    await session.execute(
                        text(
                            "UPDATE publication_range SET results=:results,version=version+1 WHERE "
                            "public_id=:id AND stage_id=:stage"
                        ),
                        {"id": token, "stage": stage_id, "results": json.dumps(state)},
                    )
            async with self.sessions() as session, session.begin():
                await self._stage(session, claim)
                row = await self._row(session, token, None, lock=True)
                failed = any(
                    row["results"]["items"].get(revision, {}).get("status") != "SUCCEEDED"
                    for revision in selected
                )
                status = "PARTIAL" if failed else "SUCCEEDED"
                await session.execute(
                    text(
                        "UPDATE publication_range SET status=:status,version=version+1 WHERE "
                        "public_id=:id AND stage_id=:stage"
                    ),
                    {"id": token, "stage": stage_id, "status": status},
                )
            await self.stages.finish(claim, {"token": token, "status": status}, datetime.now(UTC))
            return await self.recover(token, row["actor_id"])
        except Exception:
            try:
                async with self.sessions() as session, session.begin():
                    token = await self._stage(session, claim)
                    await session.execute(
                        text(
                            "UPDATE publication_range SET status='FAILED',version=version+1 "
                            "WHERE public_id=:id AND stage_id=:stage"
                        ),
                        {"id": token, "stage": stage_id},
                    )
                await self.stages.fail(
                    claim, "PUBLICATION_STAGE_FAILED", datetime.now(UTC), retry=False
                )
            except AppError:
                # A replaced or expired worker cannot alter the new owner's state.
                pass
            raise
