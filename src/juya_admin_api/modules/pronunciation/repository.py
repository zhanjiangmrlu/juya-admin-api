import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.pronunciation.domain import Config, Target
from juya_admin_api.modules.pronunciation.models import attempts, budgets, cache, targets, tasks
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class SqlPronunciationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def target(self, target_id: str) -> Target | None:
        async with self.sessions() as session:
            row = (
                (await session.execute(select(targets).where(targets.c.public_id == target_id)))
                .mappings()
                .first()
            )
            if row is None:
                return None
            return Target(**{name: row[name] for name in Target.__dataclass_fields__})

    async def cached(self, key: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = (
                (await session.execute(select(cache).where(cache.c.cache_key == key)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def task(self, task_id: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = (
                (await session.execute(select(tasks).where(tasks.c.public_id == task_id)))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def enqueue(self, target: Target, key: str, config: Config) -> dict[str, Any]:
        try:
            async with self.sessions() as session, session.begin():
                await session.execute(
                    insert(tasks).values(
                        public_id=uuid4().hex,
                        cache_key=key,
                        target_id=target.public_id,
                        config=asdict(config),
                        target_snapshot=asdict(target),
                        status="PREPARING",
                        created_at=int(time.time()),
                    )
                )
        except IntegrityError:
            pass
        async with self.sessions() as session:
            row = (
                (await session.execute(select(tasks).where(tasks.c.cache_key == key)))
                .mappings()
                .one()
            )
            return dict(row)

    async def claim(
        self, task_id: str, now: int, lease_seconds: int = 180
    ) -> dict[str, Any] | None:
        async with self.sessions() as session, session.begin():
            row = (
                (
                    await session.execute(
                        select(tasks).where(tasks.c.public_id == task_id).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["status"] not in ("PREPARING", "RETRY", "RECOVER", "RUNNING"):
                return None
            if row["lease_until"] > now or row["next_retry_at"] > now:
                return None
            if row["status"] == "RUNNING" and row["audio"] is None:
                # Worker died after dispatch: provider may have charged. Never repeat blindly.
                await session.execute(
                    update(tasks)
                    .where(tasks.c.public_id == task_id)
                    .values(
                        status="UNKNOWN", error_code="LEASE_EXPIRED_UNKNOWN", outbox_pending=False
                    )
                )
                await session.execute(
                    update(attempts)
                    .where(attempts.c.task_id == task_id, attempts.c.status == "RESERVED")
                    .values(status="UNKNOWN", error_code="LEASE_EXPIRED_UNKNOWN")
                )
                return None
            token = uuid4().hex
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == task_id)
                .values(
                    lease_token=token,
                    lease_until=now + lease_seconds,
                    status="RUNNING",
                    outbox_pending=False,
                )
            )
            return dict(row) | {"lease_token": token}

    async def reserve(
        self, job: dict[str, Any], period: str, config: Config, characters: int, now: int
    ) -> str:
        async with self.sessions() as session, session.begin():
            owned = (
                (
                    await session.execute(
                        select(tasks)
                        .where(
                            tasks.c.public_id == job["public_id"],
                            tasks.c.lease_token == job["lease_token"],
                            tasks.c.lease_until > now,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if owned is None:
                raise AppError("LEASE_LOST", "任务租约已失效", 409)
            if owned["attempt"] >= 3:
                raise AppError("RETRY_LIMIT", "已达到重试次数上限", 409)
            cost = characters * config.microcost_per_character
            result = await session.execute(
                update(budgets)
                .where(
                    budgets.c.period == period,
                    budgets.c.enabled.is_(True),
                    budgets.c.reserved_characters + characters <= budgets.c.character_limit,
                    budgets.c.reserved_cost + cost <= budgets.c.cost_limit,
                )
                .values(
                    reserved_characters=budgets.c.reserved_characters + characters,
                    reserved_cost=budgets.c.reserved_cost + cost,
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise AppError("TTS_BUDGET_EXHAUSTED", "合成预算未配置或不足", 429)
            attempt_id = uuid4().hex
            await session.execute(
                insert(attempts).values(
                    public_id=attempt_id,
                    task_id=job["public_id"],
                    attempt=owned["attempt"] + 1,
                    period=period,
                    estimated_characters=characters,
                    estimated_cost=cost,
                    price_version=config.price_version,
                    currency=config.currency,
                    region=config.region,
                    status="RESERVED",
                    created_at=now,
                )
            )
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == job["public_id"])
                .values(attempt=owned["attempt"] + 1)
            )
            return attempt_id

    async def finish_attempt(self, attempt_id: str, **values: Any) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(attempts).where(attempts.c.public_id == attempt_id).values(**values)
            )

    async def fenced_update(self, job: dict[str, Any], **values: Any) -> None:
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(tasks)
                .where(
                    tasks.c.public_id == job["public_id"],
                    tasks.c.lease_token == job["lease_token"],
                    tasks.c.lease_until > int(time.time()),
                )
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise AppError("LEASE_LOST", "任务租约已失效", 409)

    async def complete(
        self,
        job: dict[str, Any],
        object_key: str,
        duration_ms: int,
        *,
        sha256: str,
        size_bytes: int,
        content_type: str,
        security_status: str,
        security_request_id: str,
    ) -> None:
        if (
            security_status != "PASSED"
            or not security_request_id
            or duration_ms <= 0
            or size_bytes <= 0
            or len(sha256) != 64
            or not object_key.startswith("sealed/media/audio/")
            or content_type not in ("audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4")
        ):
            raise AppError("TTS_MEDIA_FACTS_INVALID", "合成媒体缺少实际检查或安全审核事实", 409)
        async with self.sessions() as session, session.begin():
            owned = (
                await session.execute(
                    select(tasks)
                    .where(
                        tasks.c.public_id == job["public_id"],
                        tasks.c.lease_token == job["lease_token"],
                        tasks.c.lease_until > int(time.time()),
                    )
                    .with_for_update()
                )
            ).first()
            if owned is None:
                raise AppError("LEASE_LOST", "任务租约已失效", 409)
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,asset_type,"
                    "content_type,size_bytes,sha256,status,security_status,"
                    "security_request_id,created_by,created_at,duration_ms) "
                    "VALUES(:id,:key,'audio',:mime,:size,:sha,'CONFIRMED','PASSED',:security,"
                    "'tts-worker',:now,:duration) ON DUPLICATE KEY UPDATE id=id"
                ),
                {
                    "id": new_ulid(datetime.now(UTC)),
                    "key": object_key,
                    "mime": content_type,
                    "size": size_bytes,
                    "sha": sha256,
                    "security": security_request_id,
                    "now": datetime.now(UTC).replace(tzinfo=None),
                    "duration": duration_ms,
                },
            )
            registered = (
                (
                    await session.execute(
                        text(
                            "SELECT object_key,status,security_status,size_bytes,"
                            "content_type,duration_ms "
                            "FROM media_asset WHERE asset_type='audio' AND sha256=:sha FOR UPDATE"
                        ),
                        {"sha": sha256},
                    )
                )
                .mappings()
                .one()
            )
            if (
                not registered["object_key"].startswith("sealed/media/audio/")
                or registered["status"] != "CONFIRMED"
                or registered["security_status"] != "PASSED"
                or registered["size_bytes"] != size_bytes
                or registered["content_type"] != content_type
                or registered["duration_ms"] != duration_ms
            ):
                raise AppError(
                    "TTS_MEDIA_REGISTRATION_CONFLICT", "已有素材未固定或检查事实不一致", 409
                )
            object_key = registered["object_key"]
            await session.execute(
                insert(cache).values(
                    cache_key=job["cache_key"],
                    object_key=object_key,
                    resource_version=job["public_id"],
                    duration_ms=duration_ms,
                    created_at=int(time.time()),
                )
            )
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == job["public_id"])
                .values(
                    status="READY",
                    object_key=object_key,
                    audio=None,
                    lease_until=0,
                    lease_token=None,
                    outbox_pending=False,
                )
            )

    async def pending(self, now: int, limit: int = 100) -> list[str]:
        async with self.sessions() as session:
            result = await session.execute(
                select(tasks.c.public_id)
                .where(
                    tasks.c.status.in_(["PREPARING", "RETRY", "RECOVER", "RUNNING"]),
                    tasks.c.next_retry_at <= now,
                    tasks.c.lease_until <= now,
                )
                .limit(limit)
            )
            return list(result.scalars())
