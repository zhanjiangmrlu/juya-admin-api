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

from juya_admin_api.infrastructure.db.v15_schema import metadata
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
        result.append(
            {
                name: candidate[name]
                for name in ("revision_id", "candidate_hash", "content_type", "content_id")
            }
            | {"group_id": candidate.get("group_id", candidate["content_type"])}
        )
    return sorted(result, key=lambda item: item["revision_id"])


class ProofreadingStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], facts: Facts | None = None):
        self.sessions = sessions
        self.facts = facts or self.default_facts

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
            existing = (
                (
                    await session.execute(
                        select(sets).where(sets.c.public_id == set_id).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                identity = normalize_candidates(existing["candidates"])
                if identity != frozen or existing["collection_hash"] != digest:
                    raise AppError("PROOFREADING_SET_CONFLICT", "校对集合不能替换候选", 409)
                for item in frozen:
                    await self.current(session, item)
                return dict(existing)
            for item in frozen:
                await self.current(session, item)
            count = sample_count(len(frozen))
            samples = secrets.SystemRandom().sample([item["revision_id"] for item in frozen], count)
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
            row = (
                (await session.execute(select(sets).where(sets.c.public_id == set_id)))
                .mappings()
                .first()
            )
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

    async def lock_set(self, session: AsyncSession, set_id: str) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    select(sets).where(sets.c.public_id == set_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("PROOFREADING_NOT_FOUND", "校对集合不存在", 404)
        return dict(row)

    async def confirm(
        self,
        set_id: str,
        revision_id: str,
        candidate_hash: str,
        actor: str,
        checks: dict[str, bool],
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            candidate = next(
                (item for item in row["candidates"] if item["revision_id"] == revision_id), None
            )
            if candidate is None or candidate["candidate_hash"] != candidate_hash:
                raise AppError("PROOFREADING_HASH_CHANGED", "校对版本与冻结集合不符", 409)
            snapshot = await self.current(session, candidate)
            facts = await self.facts(session, candidate["content_type"], revision_id, snapshot)
            required = ["text_checked"]
            if facts.get("has_audio"):
                required.append("audio_listened")
            if facts.get("has_images"):
                required.append("images_checked")
            if not facts.get("ready") or not all(checks.get(name) is True for name in required):
                raise AppError("PROOFREADING_CHECK_REQUIRED", "素材事实或人工检查不完整", 409)
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

    async def complete(self, set_id: str, actor: str) -> None:
        async with self.sessions() as session, session.begin():
            row = await self.lock_set(session, set_id)
            confirmed: set[Any] = set(
                (
                    await session.execute(
                        select(confirmations.c.revision_id).where(confirmations.c.set_id == set_id)
                    )
                ).scalars()
            )
            # Sampling never implicitly approves the rest of a cohort.
            for candidate in row["candidates"]:
                await self.current(session, candidate)
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
                {"collection_hash": row["collection_hash"]},
            )

    async def is_confirmed_within(
        self, session: AsyncSession, revision_id: str, candidate_hash: str
    ) -> bool:
        row = (
            await session.execute(
                select(confirmations).where(
                    confirmations.c.revision_id == revision_id,
                    confirmations.c.candidate_hash == candidate_hash,
                )
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

    @staticmethod
    async def default_facts(
        session: AsyncSession, content_type: str, revision_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
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
            ready = not publication_errors(ReadingContent.model_validate(snapshot))
        else:
            ready = bool(snapshot.get("title_en") and snapshot.get("title_zh"))
            if content_type == "dialogue":
                ready &= bool(snapshot.get("dialogue"))
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
            if snapshot.get("audio"):
                for sentence in snapshot.get("dialogue", []):
                    if not sentence.get("slice_version"):
                        ready = False
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
            if row is None or row["status"] != "CONFIRMED":
                ready = False
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
                or row["security_status"] != "PASSED"
                or not row["object_key"]
            ):
                ready = False
            if row:
                has_images |= row["asset_type"] == "images"
                has_audio |= row["asset_type"] == "audio"
        return {
            "ready": ready,
            "has_audio": has_audio,
            "has_images": has_images,
            "asset_count": len(asset_ids),
            "audio_version_count": len(versions),
        }
