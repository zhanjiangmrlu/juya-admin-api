"""Durable import stages; providers execute only after a fenced claim."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def _time(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    fencing_version: int
    input_hash: str
    config_version: str
    owner: str


class StageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def ensure_within(
        self, session: AsyncSession, item_id: str, stage: str, input_hash: str, config_version: str
    ) -> str:
        # Caller locks the import item so cancel and changed-input scheduling serialize.
        identifier = new_ulid(datetime.now(UTC))
        await session.execute(
            text(
                "INSERT INTO "
                "content_import_stage(public_id,item_id,stage,input_hash,config_version) "
                "VALUES(:id,:item,:stage,:hash,:config) ON DUPLICATE KEY UPDATE id=id"
            ),
            {
                "id": identifier,
                "item": item_id,
                "stage": stage,
                "hash": input_hash,
                "config": config_version,
            },
        )
        result = await session.execute(
            text(
                "SELECT public_id FROM content_import_stage WHERE item_id=:item AND stage=:stage "
                "AND input_hash=:hash AND config_version=:config FOR UPDATE"
            ),
            {"item": item_id, "stage": stage, "hash": input_hash, "config": config_version},
        )
        identifier = str(result.scalar_one())
        await session.execute(
            text(
                "UPDATE content_import_stage SET "
                "status='CANCELLED',fencing_version=fencing_version+1,"
                "lease_owner=NULL,lease_until=NULL,next_retry_at=NULL WHERE item_id=:item "
                "AND stage=:stage AND public_id<>:id AND status IN "
                "('PENDING','QUEUED','RETRY','RUNNING')"
            ),
            {"item": item_id, "stage": stage, "id": identifier},
        )
        return identifier

    async def claim(
        self, stage_id: str, owner: str, now: datetime, lease_seconds: int = 180
    ) -> Claim | None:
        if not owner or len(owner) > 64 or lease_seconds <= 0:
            raise AppError("IMPORT_STAGE_LEASE_INVALID", "阶段租约参数无效", 422)
        now = _time(now)
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE content_import_stage SET status='RUNNING',attempt=attempt+1,"
                    "fencing_version=fencing_version+1,lease_owner=:owner,lease_until=:until,"
                    "next_retry_at=NULL,output=NULL,error_code=NULL WHERE public_id=:id AND "
                    "(status='QUEUED' OR (status IN ('PENDING','RETRY') AND "
                    "(next_retry_at IS NULL OR next_retry_at<=:now)) OR "
                    "(status='RUNNING' AND lease_until<=:now))"
                ),
                {
                    "id": stage_id,
                    "owner": owner,
                    "now": now,
                    "until": now + timedelta(seconds=lease_seconds),
                },
            )
            if not getattr(result, "rowcount", 0):
                return None
            row = (
                await session.execute(
                    text(
                        "SELECT fencing_version,input_hash,config_version FROM "
                        "content_import_stage "
                        "WHERE public_id=:id"
                    ),
                    {"id": stage_id},
                )
            ).one()
            return Claim(stage_id, row.fencing_version, row.input_hash, row.config_version, owner)

    async def finish(self, claim: Claim, output: dict[str, object], now: datetime) -> None:
        await self._complete(claim, now, "SUCCEEDED", output=output)

    async def fail(self, claim: Claim, errorcode: str, now: datetime, retry: bool) -> None:
        if not errorcode or len(errorcode) > 64:
            raise AppError("IMPORT_STAGE_ERROR_INVALID", "阶段错误代码无效", 422)
        await self._complete(claim, now, "RETRY" if retry else "FAILED", errorcode=errorcode)

    async def _complete(
        self,
        claim: Claim,
        now: datetime,
        status: str,
        *,
        output: dict[str, object] | None = None,
        errorcode: str | None = None,
    ) -> None:
        now = _time(now)
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE content_import_stage SET "
                    "status=:status,output=:output,error_code=:error,"
                    "next_retry_at=:retry,lease_owner=NULL,lease_until=NULL WHERE public_id=:id "
                    "AND status='RUNNING' AND fencing_version=:fence AND lease_owner=:owner "
                    "AND lease_until>:now AND input_hash=:hash AND config_version=:config"
                ),
                {
                    "id": claim.id,
                    "fence": claim.fencing_version,
                    "owner": claim.owner,
                    "hash": claim.input_hash,
                    "config": claim.config_version,
                    "now": now,
                    "status": status,
                    "output": json.dumps(output) if output is not None else None,
                    "error": errorcode,
                    "retry": now + timedelta(seconds=30) if status == "RETRY" else None,
                },
            )
            if not getattr(result, "rowcount", 0):
                raise AppError("IMPORT_STAGE_LEASE_LOST", "阶段租约已失效", 409)

    async def cancel_item_within(self, session: AsyncSession, item_id: str) -> None:
        await session.execute(
            text(
                "UPDATE content_import_stage SET "
                "status='CANCELLED',fencing_version=fencing_version+1,"
                "lease_owner=NULL,lease_until=NULL,next_retry_at=NULL WHERE item_id=:item "
                "AND status IN ('PENDING','QUEUED','RETRY','RUNNING')"
            ),
            {"item": item_id},
        )

    async def due(self, now: datetime, limit: int = 100) -> list[str]:
        if not 1 <= limit <= 1000:
            raise AppError("IMPORT_STAGE_LIMIT_INVALID", "阶段查询上限无效", 422)
        async with self.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT public_id FROM content_import_stage WHERE "
                    "(status IN ('PENDING','RETRY') AND (next_retry_at IS NULL OR "
                    "next_retry_at<=:now)) "
                    "OR (status IN ('RUNNING','QUEUED') AND lease_until<=:now) "
                    "ORDER BY id LIMIT :limit"
                ),
                {"now": _time(now), "limit": limit},
            )
            return [str(value) for value in cast(Any, result).scalars()]

    async def enqueue(self, stage_id: str, now: datetime) -> bool:
        async with self.sessions() as session, session.begin():
            return await self.enqueue_within(session, stage_id, now)

    async def enqueue_within(self, session: AsyncSession, stage_id: str, now: datetime) -> bool:
        """Keep QUEUED/outbox atomic with the caller's enclosing command receipt."""
        now = _time(now)
        result = await session.execute(
            text(
                "UPDATE content_import_stage SET "
                "status='QUEUED',fencing_version=fencing_version+1,"
                "lease_owner=NULL,lease_until=:until WHERE public_id=:id AND "
                "((status IN ('PENDING','RETRY') AND (next_retry_at IS NULL OR "
                "next_retry_at<=:now)) "
                "OR (status IN ('RUNNING','QUEUED') AND lease_until<=:now))"
            ),
            {"id": stage_id, "now": now, "until": now + timedelta(seconds=120)},
        )
        if not getattr(result, "rowcount", 0):
            return False
        await session.execute(
            text(
                "INSERT INTO "
                "admin_outbox(event_id,event_type,aggregate_public_id,payload,status,"
                "next_attempt_at,created_at) VALUES(:event,'CONTENT_IMPORT_STAGE',:id,:payload,"
                "'PENDING',:now,:now)"
            ),
            {
                "event": new_ulid(datetime.now(UTC)),
                "id": stage_id,
                "payload": json.dumps({"stage_id": stage_id}),
                "now": now,
            },
        )
        return True
