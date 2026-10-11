"""Administrative observation and fixed preview; never accepts text or submits paid work."""

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.pronunciation.models import attempts, cache, targets, tasks
from juya_admin_api.shared.errors import AppError

AssetSigner = Callable[[str, int], Awaitable[str]]
AssetVerifier = Callable[[str], Awaitable[None]]


class PronunciationAdminStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: Config,
        *,
        sign_asset: AssetSigner,
        verify_asset: AssetVerifier,
    ):
        self.sessions, self.config = sessions, config
        self.sign_asset, self.verify_asset = sign_asset, verify_asset

    async def _target(self, session: AsyncSession, target_id: str) -> Mapping[str, Any]:
        row = (
            (await session.execute(select(targets).where(targets.c.public_id == target_id)))
            .mappings()
            .first()
        )
        if (
            row is None
            or row["status"] != "CONFIRMED"
            or row["target_type"] not in {"WORD", "CHUNK"}
        ):
            raise AppError("PRONUNCIATION_UNAVAILABLE", "发音目标不存在或未经确认", 404)
        return dict(row)

    def _key(self, target: Mapping[str, Any]) -> str:
        return cache_key(
            Target(**{name: target[name] for name in Target.__dataclass_fields__}), self.config
        )

    async def get(self, target_id: str) -> dict[str, Any]:
        async with self.sessions() as session:
            target = await self._target(session, target_id)
            key = self._key(target)
            cached = (
                (
                    await session.execute(
                        select(cache.c.resource_version, cache.c.duration_ms).where(
                            cache.c.cache_key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            job = (
                (
                    await session.execute(
                        select(
                            tasks.c.public_id,
                            tasks.c.status,
                            tasks.c.attempt,
                            tasks.c.error_code,
                            tasks.c.next_retry_at,
                        ).where(tasks.c.cache_key == key)
                    )
                )
                .mappings()
                .first()
            )
            usage = []
            if job:
                usage = [
                    dict(row)
                    for row in (
                        await session.execute(
                            select(
                                attempts.c.attempt,
                                attempts.c.status,
                                attempts.c.estimated_characters,
                                attempts.c.actual_characters,
                                attempts.c.estimated_cost,
                                attempts.c.actual_cost,
                                attempts.c.price_version,
                                attempts.c.currency,
                                attempts.c.region,
                                attempts.c.error_code,
                                attempts.c.created_at,
                            )
                            .where(attempts.c.task_id == job["public_id"])
                            .order_by(attempts.c.attempt)
                        )
                    )
                    .mappings()
                    .all()
                ]
            return {
                "target_id": target_id,
                "target_status": target["status"],
                "target_type": target["target_type"],
                "confirmed_text": target["confirmed_text"],
                "reading_variant": target["reading_variant"],
                "language": target["language"],
                "allow_synthesis": target["allow_synthesis"],
                "config_hash": key,
                "synthesis_enabled": self.config.enabled,
                "source": "UPLOADED"
                if target["uploaded_target_id"]
                else "CACHE"
                if cached
                else None,
                "resource_version": target["uploaded_resource_version"]
                if target["uploaded_target_id"]
                else cached["resource_version"]
                if cached
                else None,
                "cache": dict(cached) if cached else None,
                "task": dict(job) if job else None,
                "attempts": usage,
            }

    async def preview(self, target_id: str, resource_version: str) -> dict[str, Any]:
        async with self.sessions() as session:
            target = await self._target(session, target_id)
            if target["uploaded_target_id"]:
                if resource_version != target["uploaded_resource_version"]:
                    raise AppError(
                        "PRONUNCIATION_VERSION_MISMATCH", "预览必须绑定目标的固定资源版本", 409
                    )
                asset = (
                    (
                        await session.execute(
                            text(
                                "SELECT m.public_id AS asset_id,m.status AS asset_status,"
                                "m.security_status,m.sha256,m.object_key,m.duration_ms,"
                                "m.content_type AS mime_type,m.size_bytes,"
                                "v.status AS audio_status,t.target_type FROM audio_version v "
                                "JOIN audio_target t ON t.id=v.target_id "
                                "JOIN media_asset m ON m.id=v.asset_id "
                                "WHERE t.public_id=:target AND v.public_id=:version "
                                "AND m.asset_type='audio'"
                            ),
                            {"target": target["uploaded_target_id"], "version": resource_version},
                        )
                    )
                    .mappings()
                    .first()
                )
                source = "UPLOADED"
                allowed_types = (
                    {"WORD", "VOCABULARY", "word", "vocabulary"}
                    if target["target_type"] == "WORD"
                    else {"CHUNK", "PHRASE", "chunk", "phrase"}
                )
                if (
                    asset is None
                    or asset["target_type"] not in allowed_types
                    or asset.get("audio_status") not in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}
                ):
                    raise AppError(
                        "PRONUNCIATION_ASSET_UNAVAILABLE",
                        "固定音频不属于此词条类型或尚未人工确认",
                        409,
                    )
            else:
                key = self._key(target)
                asset = (
                    (
                        await session.execute(
                            text(
                                "SELECT m.public_id AS asset_id,m.status AS asset_status,"
                                "m.security_status,m.sha256,m.object_key,m.duration_ms,"
                                "m.content_type AS mime_type,m.size_bytes "
                                "FROM pronunciation_cache c JOIN media_asset m "
                                "ON m.object_key=c.object_key "
                                "WHERE c.cache_key=:key AND c.resource_version=:version "
                                "AND m.asset_type='audio'"
                            ),
                            {"key": key, "version": resource_version},
                        )
                    )
                    .mappings()
                    .first()
                )
                source = "CACHE"
            if (
                not asset
                or asset["asset_status"] != "CONFIRMED"
                or asset["security_status"] != "PASSED"
                or not asset["object_key"]
                or not asset["sha256"]
                or not re.fullmatch(r"[0-9a-fA-F]{64}", asset["sha256"])
                or not asset["duration_ms"]
                or asset["duration_ms"] <= 0
                or not asset["size_bytes"]
                or asset["size_bytes"] <= 0
                or not str(asset["mime_type"]).startswith("audio/")
            ):
                raise AppError(
                    "PRONUNCIATION_ASSET_UNAVAILABLE", "固定音频缺少已审核实际媒体事实", 409
                )
            asset_id = asset["asset_id"]
            facts = {
                name: asset[name] for name in ("duration_ms", "mime_type", "size_bytes", "sha256")
            }
        await self.verify_asset(asset_id)
        url = await self.sign_asset(asset_id, 60)
        return {
            "target_id": target_id,
            "resource_version": resource_version,
            "source": source,
            "url": url,
            "expires_in": 60,
            **facts,
        }

    async def retry(self, task_id: str) -> dict[str, Any]:
        # Local recovery reuses retained paid bytes. No API can reset UNKNOWN or buy another call.
        async with self.sessions() as session, session.begin():
            job = (
                (
                    await session.execute(
                        select(tasks).where(tasks.c.public_id == task_id).with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not job:
                raise AppError("PRONUNCIATION_TASK_NOT_FOUND", "发音任务不存在", 404)
            if (
                job["status"] != "RECOVER"
                or not job["audio"]
                or job["lease_until"] > int(time.time())
            ):
                raise AppError(
                    "PRONUNCIATION_RETRY_UNSAFE",
                    "仅可重试已留存音频的本地恢复,未知付费结果禁止重试",
                    409,
                )
            await session.execute(
                update(tasks)
                .where(tasks.c.public_id == task_id)
                .values(outbox_pending=True, next_retry_at=0, lease_token=None, lease_until=0)
            )
            return {"task_id": task_id, "status": "RECOVER", "paid_retry": False}
