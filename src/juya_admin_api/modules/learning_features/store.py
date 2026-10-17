"""Independent authoritative learning switches; invalid fields individually fail closed."""

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.production_store import ProductionStore, decode, encode
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

CONFIG_KEY = "learning_features_v15"


def configuration(value: Any, version: int) -> dict[str, Any]:
    try:
        value = decode(value)
    except (ValueError, TypeError):
        value = {}
    return {
        "recording_enabled": isinstance(value, dict) and value.get("recording_enabled") is True,
        "profile_completion_enabled": isinstance(value, dict)
        and value.get("profile_completion_enabled") is True,
        "version": version,
    }


class LearningFeaturesStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.commands = ProductionStore(sessions)

    @staticmethod
    async def read_within(session: AsyncSession, *, lock: bool = False) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT value,version FROM system_config WHERE config_key=:key"
                        + (" FOR SHARE" if lock else "")
                    ),
                    {"key": CONFIG_KEY},
                )
            )
            .mappings()
            .first()
        )
        return configuration(row["value"], row["version"]) if row else configuration({}, 0)

    async def read(self) -> dict[str, Any]:
        async with self.sessions() as session:
            return await self.read_within(session)

    async def command(self, actor: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not key or len(key) > 100:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "需要有效幂等键", 422)
        if (
            type(payload.get("recording_enabled")) is not bool
            or type(payload.get("expected_version")) is not int
            or payload["expected_version"] < 0
            or (
                "profile_completion_enabled" in payload
                and type(payload["profile_completion_enabled"]) is not bool
            )
            or bool(
                set(payload)
                - {"recording_enabled", "profile_completion_enabled", "expected_version"}
            )
        ):
            raise AppError("LEARNING_FEATURES_INVALID", "学习功能开关或版本无效", 422)
        digest = snapshot_hash(payload)
        async with self.sessions() as session, session.begin():
            replay = await self.commands.creation_replay(
                session, CONFIG_KEY, actor, key, digest, lock=False
            )
            if replay is not None:
                return replay
            # The unique key also serializes concurrent first creation; no enabled default.
            await session.execute(
                text(
                    "INSERT INTO system_config(config_key,value,version) "
                    "VALUES(:key,:value,0) ON DUPLICATE KEY UPDATE config_key=config_key"
                ),
                {
                    "key": CONFIG_KEY,
                    "value": encode(
                        {"recording_enabled": False, "profile_completion_enabled": False}
                    ),
                },
            )
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT value,version FROM system_config "
                            "WHERE config_key=:key FOR UPDATE"
                        ),
                        {"key": CONFIG_KEY},
                    )
                )
                .mappings()
                .one()
            )
            replay = await self.commands.creation_replay(session, CONFIG_KEY, actor, key, digest)
            if replay is not None:
                return replay
            if row["version"] != payload["expected_version"]:
                raise AppError("CONFIG_VERSION_CONFLICT", "学习功能配置已变化", 409)
            previous = configuration(row["value"], row["version"])
            result = {
                "recording_enabled": payload["recording_enabled"],
                "profile_completion_enabled": payload.get(
                    "profile_completion_enabled", previous["profile_completion_enabled"]
                ),
                "version": row["version"] + 1,
            }
            now = datetime.now(UTC)
            await session.execute(
                text(
                    "UPDATE system_config SET value=:value,version=:version,updated_by=:actor,"
                    "updated_at=:now WHERE config_key=:key AND version=:expected"
                ),
                {
                    "value": encode(
                        {
                            name: result[name]
                            for name in ("recording_enabled", "profile_completion_enabled")
                        }
                    ),
                    "version": result["version"],
                    "actor": actor,
                    "now": now,
                    "key": CONFIG_KEY,
                    "expected": payload["expected_version"],
                },
            )
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,actor_public_id,action,object_type,"
                    "object_public_id,before_summary,after_summary,request_id,created_at) "
                    "VALUES(:id,:actor,'learning_features.update','system_config',:object,"
                    ":before,:after,:request,:now)"
                ),
                {
                    "id": new_ulid(now),
                    "actor": actor,
                    "object": CONFIG_KEY,
                    "before": encode(previous),
                    "after": encode(result),
                    "request": key if len(key) <= 64 else hashlib.sha256(key.encode()).hexdigest(),
                    "now": now,
                },
            )
            await self.commands.remember_creation(
                session, CONFIG_KEY, actor, key, digest, result, now
            )
            return result
