"""Immutable review cohorts with server-chosen samples and hash-bound human approval."""

import hashlib
import json
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.infrastructure.db.v15_schema import metadata
from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.short_reading.schemas import ReadingContent, publication_errors
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

sets = metadata.tables["proofreading_set"]
confirmations = metadata.tables["proofreading_confirmation"]
Facts = Callable[[AsyncSession, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def sample_count(count: int) -> int:
    if count < 0:
        raise ValueError("count cannot be negative")
    return 0 if count == 0 else min(count, 20, max(1, math.ceil(count * 0.05)))


def snapshot_hash(snapshot: Any) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def collection_hash(revisions: Mapping[str, str]) -> str:
    return snapshot_hash(dict(revisions))


def normal_candidate(facts: dict[str, Any], issues: list[Any]) -> bool:
    return bool(facts.get("normal", facts.get("ready", False))) and not issues


def normalize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for candidate in candidates:
        revision = candidate["revision_id"]
        if revision in seen:
            raise AppError("DUPLICATE_CANDIDATE", "候选版本重复", 422)
        seen.add(revision)
        if candidate["content_type"] not in ("dialogue", "word_card", "short_reading"):
            raise AppError("INVALID_CONTENT_TYPE", "校对类型无效", 422)
        group_id = candidate["content_type"]
        if candidate.get("group_id", group_id) != group_id:
            raise AppError("PROOFREADING_GROUP_MISMATCH", "校对分组由服务端内容类型固定", 422)
        result.append(
            {
                name: candidate[name]
                for name in ("revision_id", "candidate_hash", "content_type", "content_id")
            }
            | {"group_id": group_id}
        )
    return sorted(result, key=lambda item: item["revision_id"])


class ProofreadingStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        facts: Facts | None = None,
        *,
        require_review: bool = True,
    ):
        # sessions 管理校对事务,facts 可注入事实校验,require_review 遵循实际环境的审核策略
        self.sessions = sessions
        self.require_review = require_review
        self.facts = facts or self.default_facts

    async def classify_within(
        self, session: AsyncSession, candidate: dict[str, Any], snapshot: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        facts = await self.facts(
            session, candidate["content_type"], candidate["revision_id"], snapshot
        )
        rows: Sequence[Any] = (
            (
                await session.execute(
                    text("SELECT issues FROM content_import_item WHERE revision_id=:id FOR SHARE"),
                    {"id": candidate["revision_id"]},
                )
            )
            .scalars()
            .all()
        )
        issues: list[Any] = []
        for value in rows:
            issues.extend(json.loads(value) if isinstance(value, str) else (value or []))
        return facts, normal_candidate(facts, issues)

    async def audit(
        self, session: AsyncSession, set_id: str, actor: str, action: str, summary: dict[str, Any]
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO audit_event (public_id,actor_public_id,action,"
                "object_type,object_public_id,before_summary,after_summary,"
                "reason,request_id,created_at) "
                "VALUES (:id,:actor,:action,'proofreading_set',:set_id,'{}',:summary,NULL,:id,:now)"
            ),
            {
                "id": new_ulid(datetime.now(UTC)),
                "actor": actor,
                "action": action,
                "set_id": set_id,
                "summary": json.dumps(summary),
                "now": datetime.now(UTC).replace(tzinfo=None),
            },
        )

    async def current(self, session: AsyncSession, candidate: dict[str, Any]) -> dict[str, Any]:
        short = candidate["content_type"] == "short_reading"
        sql = (
            (
                "SELECT r.content_snapshot,r.content_hash FROM short_reading_revision r "
                "JOIN short_reading c ON c.public_id=r.short_reading_id "
                "WHERE r.public_id=:revision AND c.public_id=:content "
                "AND c.draft_revision_id=r.public_id FOR UPDATE"
            )
            if short
            else (
                "SELECT r.content_snapshot FROM scene_revision r JOIN scene c ON c.id=r.scene_id "
                "WHERE r.public_id=:revision AND c.public_id=:content "
                "AND c.draft_revision_id=r.id FOR UPDATE"
            )
        )
        row = (
            (
                await session.execute(
                    text(sql),
                    {"revision": candidate["revision_id"], "content": candidate["content_id"]},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("PROOFREADING_REVISION_STALE", "候选版本已不是当前草稿", 409)
        snapshot = row["content_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        actual_hash = row["content_hash"] if short else snapshot_hash(snapshot)
        if actual_hash != candidate["candidate_hash"] or snapshot_hash(snapshot) != actual_hash:
            raise AppError("PROOFREADING_HASH_CHANGED", "候选内容已变化,请重新创建校对集合", 409)
        return dict(snapshot)

    async def create(
        self, candidates: list[dict[str, Any]], actor: str, *, set_id: str | None = None
    ) -> dict[str, Any]:
        frozen = normalize_candidates(candidates)
        digest = collection_hash({item["revision_id"]: item["candidate_hash"] for item in frozen})
        set_id = set_id or new_ulid(datetime.now(UTC))
        async with self.sessions() as session, session.begin():
            existing = await self.read_set(session, set_id, lock=True)
            if existing:
                identity = normalize_candidates(existing["candidates"])
                if identity != frozen or existing["collection_hash"] != digest:
                    raise AppError("PROOFREADING_SET_CONFLICT", "校对集合不能替换候选", 409)
                for item in frozen:
                    await self.current(session, item)
                return dict(existing)
            for item in frozen:
                snapshot = await self.current(session, item)
                facts, normal = await self.classify_within(session, item, snapshot)
                item["normal"] = normal
                item["classification_facts"] = facts
            normal_ids = [item["revision_id"] for item in frozen if item["normal"]]
            count = len(normal_ids)
            samples = secrets.SystemRandom().sample(normal_ids, sample_count(count))
            await session.execute(
                insert(sets).values(
                    public_id=set_id,
                    collection_hash=digest,
                    candidates=frozen,
                    normal_count=count,
                    sample_ids=samples,
                    status="PENDING",
                    created_by=actor,
                )
            )
            await self.audit(
                session,
                set_id,
                actor,
                "proofreading.create",
                {"collection_hash": digest, "normal_count": count},
            )
            return {
                "public_id": set_id,
                "collection_hash": digest,
                "candidates": frozen,
                "normal_count": count,
                "sample_ids": samples,
                "status": "PENDING",
            }

    async def get(self, set_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = await self.read_set(session, set_id)
            if row is None:
                raise AppError("PROOFREADING_NOT_FOUND", "校对集合不存在", 404)
            confirmed: Sequence[Any] = (
                (
                    await session.execute(
                        select(confirmations.c.revision_id).where(confirmations.c.set_id == set_id)
                    )
                )
                .scalars()
                .all()
            )
            return dict(row) | {"confirmed_ids": list(confirmed)}

    async def append_samples(self, set_id: str, revision_ids: list[str], actor: str) -> None:
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            candidates = {item["revision_id"]: item for item in row["candidates"]}
            if any(revision not in candidates for revision in revision_ids):
                raise AppError("PROOFREADING_INVALID_SAMPLE", "追加样本不属于冻结集合", 422)
            for revision in revision_ids:
                await self.current(session, candidates[revision])
            samples = list(dict.fromkeys([*row["sample_ids"], *revision_ids]))
            await session.execute(
                update(sets)
                .where(sets.c.public_id == set_id)
                .values(sample_ids=samples, status="PENDING")
            )
            await self.audit(session, set_id, actor, "proofreading.append", {"sample_ids": samples})

    async def read_set(
        self, session: AsyncSession, set_id: str, *, lock: bool = False
    ) -> dict[str, Any] | None:
        columns = [column for column in sets.c if column.name not in {"candidates", "sample_ids"}]
        row = (
            (
                await session.execute(
                    select(*columns)
                    .where(sets.c.public_id == set_id)
                    .with_for_update(read=not lock)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = dict(row)
        for column in ("candidates", "sample_ids"):
            result[column] = await read_json(
                session, "proofreading_set", column, {"public_id": set_id}
            )
        return result

    async def lock_set(self, session: AsyncSession, set_id: str) -> dict[str, Any]:
        row = await self.read_set(session, set_id, lock=True)
        if row is None:
            raise AppError("PROOFREADING_NOT_FOUND", "校对集合不存在", 404)
        return row

    async def confirm(
        self,
        set_id: str,
        revision_id: str,
        candidate_hash: str,
        actor: str,
        checks: dict[str, bool],
    ) -> None:
        # set_id/revision_id/candidate_hash 固定集合及草稿
        # actor 为实际检查人,checks 为显式人工检查
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            candidate = next(
                (item for item in row["candidates"] if item["revision_id"] == revision_id), None
            )
            if candidate is None or candidate["candidate_hash"] != candidate_hash:
                raise AppError("PROOFREADING_HASH_CHANGED", "校对版本与冻结集合不符", 409)
            snapshot = await self.current(session, candidate)
            facts = await self.facts(session, candidate["content_type"], revision_id, snapshot)
            self.check_frozen_facts(candidate, facts)
            required = ["text_checked"]
            if facts.get("has_audio"):
                required.append("audio_listened")
            if facts.get("has_images"):
                required.append("images_checked")
            if not facts.get("ready") or not all(checks.get(name) is True for name in required):
                raise AppError("PROOFREADING_CHECK_REQUIRED", "素材事实或人工检查不完整", 409)
            await self.approve_audio_within(session, set_id, candidate, facts, actor, "MANUAL")
            await session.execute(
                delete(confirmations).where(
                    confirmations.c.set_id == set_id, confirmations.c.revision_id == revision_id
                )
            )
            await session.execute(
                insert(confirmations).values(
                    set_id=set_id,
                    content_type=candidate["content_type"],
                    content_id=candidate["content_id"],
                    revision_id=revision_id,
                    candidate_hash=candidate_hash,
                    method="MANUAL",
                    confirmed_by=actor,
                    confirmed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            for item in row["candidates"]:
                if item["revision_id"] == revision_id:
                    item["inspection"] = {"checks": checks, "facts": facts, "actor": actor}
                    item.pop("failure", None)
            await session.execute(
                update(sets)
                .where(sets.c.public_id == set_id)
                .values(candidates=row["candidates"], status="PENDING")
            )
            await self.audit(
                session,
                set_id,
                actor,
                "proofreading.confirm",
                {
                    "revision_id": revision_id,
                    "candidate_hash": candidate_hash,
                    "checks": checks,
                    "facts": facts,
                },
            )

    async def fail(self, set_id: str, revision_id: str, error_code: str, actor: str) -> None:
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            candidate = next(
                (item for item in row["candidates"] if item["revision_id"] == revision_id), None
            )
            if candidate is None:
                raise AppError("PROOFREADING_INVALID_SAMPLE", "失败条目不属于集合", 422)
            affected = [
                item["revision_id"]
                for item in row["candidates"]
                if item["group_id"] == candidate["group_id"]
            ]
            affected_sets: list[Any] = list(
                (
                    await session.execute(
                        select(confirmations.c.set_id).where(
                            confirmations.c.revision_id.in_(affected)
                        )
                    )
                ).scalars()
            )
            await session.execute(
                delete(confirmations).where(confirmations.c.revision_id.in_(affected))
            )
            if affected_sets:
                await session.execute(
                    update(sets).where(sets.c.public_id.in_(affected_sets)).values(status="FAILED")
                )
            for item in row["candidates"]:
                if item["revision_id"] in affected:
                    item["failure"] = {"error_code": error_code, "reported_revision": revision_id}
                    item.pop("inspection", None)
            await session.execute(
                update(sets)
                .where(sets.c.public_id == set_id)
                .values(candidates=row["candidates"], status="FAILED")
            )
            await self.audit(
                session,
                set_id,
                actor,
                "proofreading.fail",
                {"affected": affected, "error_code": error_code},
            )

    async def bulk_confirm(
        self,
        set_id: str,
        actor: str,
        collection_digest: str,
        revision_ids: list[str],
        accept_normal_results: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        # set_id/collection_digest/revision_ids 固定正常集合,actor 为抽样后的审批人
        # accept_normal_results 必须显式接受正常结果,idempotency_key 保持同次审批幂等
        from juya_admin_api.modules.content.production_store import ProductionStore

        if accept_normal_results is not True:
            raise AppError("PROOFREADING_BATCH_ACCEPT_REQUIRED", "必须显式确认正常结构化结果", 422)
        if not revision_ids or len(set(revision_ids)) != len(revision_ids):
            raise AppError("PROOFREADING_BATCH_SELECTION_INVALID", "批量确认范围无效", 422)
        async with self.sessions() as session, session.begin():
            commands = ProductionStore(self.sessions)
            scope = "proofreading.normal:" + set_id
            request_hash = snapshot_hash(
                {
                    "collection": collection_digest,
                    "revision_ids": sorted(revision_ids),
                    "accept_normal_results": accept_normal_results,
                }
            )
            key = idempotency_key or request_hash
            if not key or len(key) > 100:
                raise AppError("IDEMPOTENCY_KEY_INVALID", "批量确认幂等键无效", 422)
            replay = await commands.creation_replay(session, scope, actor, key, request_hash)
            if replay is not None:
                return replay
            row = await self.lock_set(session, set_id)
            if row["collection_hash"] != collection_digest:
                raise AppError("PROOFREADING_HASH_CHANGED", "校对集合已变化", 409)
            candidates = {item["revision_id"]: item for item in row["candidates"]}
            if any(identity not in candidates for identity in revision_ids):
                raise AppError(
                    "PROOFREADING_BATCH_SELECTION_INVALID", "确认项目不属于冻结集合", 422
                )
            manual_rows = (
                await session.execute(
                    select(confirmations.c.revision_id, confirmations.c.candidate_hash)
                    .where(confirmations.c.set_id == set_id, confirmations.c.method == "MANUAL")
                    .with_for_update()
                )
            ).all()
            manual = {item.revision_id: item.candidate_hash for item in manual_rows}
            if not row["sample_ids"] and any(item.get("normal") for item in candidates.values()):
                raise AppError("PROOFREADING_SAMPLE_REQUIRED", "正常集合尚未完成服务端抽样", 409)
            for identity in row["sample_ids"]:
                sample = candidates[identity]
                sample_snapshot = await self.current(session, sample)
                if sample.get("failure") or manual.get(identity) != sample["candidate_hash"]:
                    raise AppError(
                        "PROOFREADING_SAMPLE_REQUIRED", "必须先人工检查全部冻结样本", 409
                    )
                sample_facts, sample_normal = await self.classify_within(
                    session, sample, sample_snapshot
                )
                self.check_frozen_facts(sample, sample_facts)
                if not sample_normal or not sample_facts.get("ready"):
                    raise AppError("PROOFREADING_CHECK_REQUIRED", "已检查样本的资源事实失效", 409)
            selected = [candidates[identity] for identity in revision_ids]
            for item in selected:
                if not item.get("normal") or item.get("failure"):
                    raise AppError(
                        "PROOFREADING_ABNORMAL_REQUIRES_MANUAL", "异常项目必须逐项人工校对", 409
                    )
                snapshot = await self.current(session, item)
                facts, normal = await self.classify_within(session, item, snapshot)
                self.check_frozen_facts(item, facts)
                if not normal or not facts.get("ready"):
                    raise AppError(
                        "PROOFREADING_CHECK_REQUIRED", "正常项目素材事实已变化或尚未准备", 409
                    )
                if manual.get(item["revision_id"]) == item["candidate_hash"]:
                    continue
                await self.approve_audio_within(session, set_id, item, facts, actor, "BATCH_NORMAL")
                await session.execute(
                    delete(confirmations).where(
                        confirmations.c.set_id == set_id,
                        confirmations.c.revision_id == item["revision_id"],
                    )
                )
                await session.execute(
                    insert(confirmations).values(
                        set_id=set_id,
                        content_type=item["content_type"],
                        content_id=item["content_id"],
                        revision_id=item["revision_id"],
                        candidate_hash=item["candidate_hash"],
                        method="BATCH_NORMAL",
                        confirmed_by=actor,
                        confirmed_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                item["inspection"] = {"method": "BATCH_NORMAL", "facts": facts, "actor": actor}
            await session.execute(
                update(sets)
                .where(sets.c.public_id == set_id)
                .values(
                    candidates=list(candidates.values()),
                    status="PENDING",
                )
            )
            await self.audit(
                session,
                set_id,
                actor,
                "proofreading.bulk_confirm",
                {
                    "collection_hash": collection_digest,
                    "revision_ids": revision_ids,
                    "accept_normal_results": True,
                },
            )
            result = {"confirmed_ids": revision_ids, "collection_hash": collection_digest}
            await commands.remember_creation(
                session, scope, actor, key, request_hash, result, datetime.now(UTC)
            )
            return result

    @staticmethod
    def check_frozen_facts(candidate: dict[str, Any], facts: dict[str, Any]) -> None:
        # candidate 为冻结候选与准备事实,facts 为当前锁定资源事实,合法核准不改变准备摘要
        frozen = candidate.get("classification_facts", {}).get("preparation_hash")
        if frozen and facts.get("preparation_hash") != frozen:
            raise AppError(
                "PROOFREADING_MATERIAL_CHANGED", "冻结校对资源已变化,请重建校对集合", 409
            )

    async def approve_audio_within(
        self,
        session: AsyncSession,
        set_id: str,
        candidate: dict[str, Any],
        facts: dict[str, Any],
        actor: str,
        method: str,
    ) -> None:
        # session 为同一审批事务,set_id/candidate 固定核准归属,facts 为已校验和加锁的资源
        # actor 为审批人,method 区分实际人工样本检查与正常集合批量核准,不改正文或伪造逐句试听
        versions = facts.get("audio_version_ids", [])
        slices = facts.get("slice_ids", [])
        if not versions and not slices:
            return
        if not actor or method not in {"MANUAL", "BATCH_NORMAL"} or not facts.get("ready"):
            raise AppError(
                "PROOFREADING_CHECK_REQUIRED", "音频核准需要有效检查人和完整准备事实", 409
            )
        for version in versions:
            await session.execute(
                text(
                    "UPDATE audio_version SET status='CONFIRMED' "
                    "WHERE public_id=:id AND status='READY'"
                ),
                {"id": version},
            )
        for identity in slices:
            await session.execute(
                text(
                    "UPDATE sentence_audio_slice SET status='CONFIRMED',confirmed_by=:actor,"
                    "confirmed_at=UTC_TIMESTAMP(6) WHERE public_id=:id "
                    "AND revision_id=:revision AND status='READY'"
                ),
                {"id": identity, "revision": candidate["revision_id"], "actor": actor},
            )
        await self.audit(
            session,
            set_id,
            actor,
            "proofreading.audio_approve",
            {
                "revision_id": candidate["revision_id"],
                "candidate_hash": candidate["candidate_hash"],
                "preparation_hash": facts.get("preparation_hash"),
                "actor": actor,
                "method": method,
                "reason": "人工样本音频检查确认"
                if method == "MANUAL"
                else "冻结集合抽样通过后的正常音频批量核准",
                "audio_version_ids": versions,
                "slice_ids": slices,
            },
        )

    async def complete(
        self, set_id: str, actor: str, *, allow_abnormal_drafts: bool = False
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            confirmed: set[Any] = set(
                (
                    await session.execute(
                        select(confirmations.c.revision_id).where(confirmations.c.set_id == set_id)
                    )
                ).scalars()
            )
            held_drafts = []
            # Only explicit batch/manual confirmation creates qualifying confirmation rows.
            for candidate in row["candidates"]:
                await self.current(session, candidate)
                if (
                    allow_abnormal_drafts
                    and candidate.get("normal") is False
                    and candidate["revision_id"] not in confirmed
                ):
                    held_drafts.append(candidate["revision_id"])
                    continue
                if candidate["revision_id"] not in confirmed or candidate.get("failure"):
                    raise AppError("PROOFREADING_INCOMPLETE", "整组必须逐项确认,不能自动通过", 409)
            await session.execute(
                update(sets).where(sets.c.public_id == set_id).values(status="CONFIRMED")
            )
            await self.audit(
                session,
                set_id,
                actor,
                "proofreading.complete",
                {"collection_hash": row["collection_hash"], "held_drafts": held_drafts},
            )

    async def is_confirmed_within(
        self, session: AsyncSession, revision_id: str, candidate_hash: str
    ) -> bool:
        row = (
            await session.execute(
                select(confirmations)
                .join(sets, confirmations.c.set_id == sets.c.public_id)
                .where(
                    confirmations.c.revision_id == revision_id,
                    confirmations.c.candidate_hash == candidate_hash,
                    sets.c.status == "CONFIRMED",
                )
                .with_for_update(read=True)
            )
        ).first()
        return row is not None

    async def invalidate_within(self, session: AsyncSession, revision_id: str) -> None:
        set_ids: list[Any] = list(
            (
                await session.execute(
                    select(confirmations.c.set_id).where(confirmations.c.revision_id == revision_id)
                )
            ).scalars()
        )
        await session.execute(
            delete(confirmations).where(confirmations.c.revision_id == revision_id)
        )
        if set_ids:
            await session.execute(
                update(sets).where(sets.c.public_id.in_(set_ids)).values(status="PENDING")
            )

    async def default_facts(
        self, session: AsyncSession, content_type: str, revision_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        # session 读取真实素材,content_type/revision_id/snapshot 固定候选版本和条件式发布规则
        if content_type != "short_reading" and snapshot.get("schema_version") == 2:
            from juya_admin_api.modules.content.v15_scene_audio_facts import scene_preparation_facts

            return await scene_preparation_facts(
                self.sessions,
                session,
                content_type,
                revision_id,
                snapshot,
                require_review=self.require_review,
            )
        asset_ids: set[str] = set()
        versions: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and item:
                        if key in (
                            "asset_id",
                            "icon_asset_id",
                            "cover_asset_id",
                            "original_image_asset_id",
                        ):
                            asset_ids.add(item)
                        if key in ("audio_version_id", "version_id"):
                            versions.add(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(snapshot)
        if content_type == "short_reading":
            errors = publication_errors(ReadingContent.model_validate(snapshot))
            ready = not errors
            normal = not [code for code in errors if code != "AUDIO_LISTENING_REQUIRED"]
        else:
            ready = bool(snapshot.get("title_en") and snapshot.get("title_zh"))
            normal = bool(ready and snapshot.get("copyright") and snapshot.get("source"))
            if content_type == "dialogue":
                ready &= bool(snapshot.get("dialogue"))
                normal &= bool(snapshot.get("dialogue")) and all(
                    row.get("speaker") and row.get("english") and row.get("chinese")
                    for row in snapshot.get("dialogue", [])
                )
            else:
                normal &= bool(snapshot.get("vocabulary"))
            for entry in [*snapshot.get("vocabulary", []), *snapshot.get("chunks", [])]:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT confirmed_by,confirmed_at FROM content_entry_revision "
                                "WHERE revision_id=:revision AND entry_id=:entry FOR UPDATE"
                            ),
                            {"revision": revision_id, "entry": entry.get("entry_id")},
                        )
                    )
                    .mappings()
                    .first()
                )
                ready &= bool(row and row["confirmed_by"] and row["confirmed_at"])
                normal &= bool(
                    row
                    and entry.get("english")
                    and entry.get("chinese")
                    and entry.get("sense_id")
                    and entry.get("source_id")
                )
            if snapshot.get("audio"):
                for sentence in snapshot.get("dialogue", []):
                    if not sentence.get("slice_version"):
                        ready = False
                        normal = False
                        continue
                    row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT status FROM sentence_audio_slice "
                                    "WHERE revision_id=:revision AND sentence_id=:sentence "
                                    "AND resource_version=:version FOR UPDATE"
                                ),
                                {
                                    "revision": revision_id,
                                    "sentence": sentence["id"],
                                    "version": sentence["slice_version"],
                                },
                            )
                        )
                        .mappings()
                        .first()
                    )
                    ready &= bool(row and row["status"] == "CONFIRMED")
        has_audio = bool(versions)
        has_images = False
        for version in sorted(versions):
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT v.status,m.public_id AS asset_id "
                            "FROM audio_version v JOIN media_asset m ON m.id=v.asset_id "
                            "WHERE v.public_id=:id FOR UPDATE"
                        ),
                        {"id": version},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] not in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}:
                ready = False
            normal &= bool(row and row["status"] in {"READY", "CONFIRMED", "ACTIVE", "SUPERSEDED"})
            if row:
                asset_ids.add(row["asset_id"])
        for asset_id in sorted(asset_ids):
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT status,security_status,asset_type,object_key "
                            "FROM media_asset WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": asset_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or row["status"] != "CONFIRMED"
                or not security_status_usable(
                    row["security_status"], require_review=self.require_review
                )
                or not row["object_key"]
            ):
                ready = False
                normal = False
            if row:
                has_images |= row["asset_type"] == "images"
                has_audio |= row["asset_type"] == "audio"
        return {
            "ready": ready,
            "normal": normal,
            "has_audio": has_audio,
            "has_images": has_images,
            "asset_count": len(asset_ids),
            "audio_version_count": len(versions),
        }
