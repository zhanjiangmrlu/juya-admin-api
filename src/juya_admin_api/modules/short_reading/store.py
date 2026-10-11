"""Transactional short readings, immutable publications and typed package membership."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.content.production_store import ProductionStore, decode, encode
from juya_admin_api.modules.content.v15_contracts import ResourceReference, ShortReadingDetail
from juya_admin_api.modules.short_reading.schemas import ReadingContent, publication_errors
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

Confirmation = Callable[[AsyncSession, str, str], Awaitable[bool]]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class ShortReadingStore:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        check_confirmation: Confirmation | None = None,
        require_review: bool = True,
    ) -> None:
        self.sessions = sessions
        self.check_confirmation = check_confirmation
        self.require_review = require_review
        self.commands = ProductionStore(sessions)

    async def create_within(
        self,
        session: AsyncSession,
        series_id: str,
        content: dict[str, Any],
        actor_id: str,
        identity: str | None = None,
    ) -> dict[str, Any]:
        if not await session.scalar(
            text("SELECT public_id FROM content_series WHERE public_id=:id FOR UPDATE"),
            {"id": series_id},
        ):
            raise AppError("SERIES_NOT_FOUND", "内容系列不存在", 404)
        now = datetime.now(UTC)
        identity = identity or new_ulid(now)
        revision = new_ulid(now)
        snapshot = self.normalize(content)
        await session.execute(
            text(
                "INSERT INTO short_reading(public_id,series_id,status,"
                "draft_revision_id,version,created_at,updated_at) "
                "VALUES(:id,:series,'DRAFT',:revision,1,:now,:now)"
            ),
            {"id": identity, "series": series_id, "revision": revision, "now": now},
        )
        await self.insert_revision(session, identity, revision, snapshot, actor_id, 1, None)
        await session.execute(
            text(
                "INSERT INTO content_identity(content_type,content_id,"
                "created_at) VALUES('short_reading',:id,:now)"
            ),
            {"id": identity, "now": now},
        )
        return {"content_id": identity, "revision_id": revision, "version": 1}

    @staticmethod
    def normalize(content: dict[str, Any]) -> dict[str, Any]:
        model = ReadingContent.model_validate(content)
        if model.paragraph_ids and (
            len(model.paragraph_ids) != len(model.paragraphs_en)
            or len(set(model.paragraph_ids)) != len(model.paragraph_ids)
        ):
            raise AppError("PARAGRAPH_ID_INVALID", "自然段标识无效", 422)
        if not model.paragraph_ids:
            model.paragraph_ids = [new_ulid(datetime.now(UTC)) for _ in model.paragraphs_en]
        return model.model_dump(mode="json")

    async def insert_revision(
        self,
        session: AsyncSession,
        identity: str,
        revision: str,
        content: dict[str, Any],
        actor: str,
        number: int,
        source: str | None,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO short_reading_revision(public_id,"
                "short_reading_id,source_revision_id,version_no,edit_version,status,"
                "content_snapshot,content_hash,created_by,created_at) "
                "VALUES(:id,:reading,:source,:number,1,'DRAFT',:content,:hash,:actor,:now)"
            ),
            {
                "id": revision,
                "reading": identity,
                "source": source,
                "number": number,
                "content": encode(content),
                "hash": digest(content),
                "actor": actor,
                "now": datetime.now(UTC),
            },
        )

    async def row(self, session: AsyncSession, identity: str, lock: bool = False) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT s.*,COALESCE(t.enabled,0) AS trial_enabled FROM short_reading s "
                        "LEFT JOIN short_reading_trial t ON t.short_reading_id=s.public_id "
                        "WHERE s.public_id=:id" + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("SHORT_READING_NOT_FOUND", "短文不存在", 404)
        return {**dict(row), "trial_enabled": bool(row["trial_enabled"])}

    async def revision(
        self, session: AsyncSession, identity: str, revision: str, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM short_reading_revision "
                        "WHERE public_id=:revision AND short_reading_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": identity, "revision": revision},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("REVISION_NOT_FOUND", "短文版本不存在", 404)
        result = dict(row)
        result["content_snapshot"] = decode(result["content_snapshot"])
        return result

    async def inspect(self, session: AsyncSession, revision: dict[str, Any]) -> list[str]:
        content = ReadingContent.model_validate(revision["content_snapshot"])
        errors = publication_errors(content)
        if self.check_confirmation is None or not await self.check_confirmation(
            session, revision["public_id"], revision["content_hash"]
        ):
            errors.append("PROOFREADING_REQUIRED")
        if content.audio:
            audio = content.audio
            fact = (
                (
                    await session.execute(
                        text(
                            "SELECT v.status AS version_status,"
                            "t.public_id AS target_id,m.public_id AS asset_id,"
                            "m.status,m.security_status,"
                            "m.asset_type,m.duration_ms FROM audio_version v JOIN audio_target t "
                            "ON t.id=v.target_id JOIN media_asset m ON m.id=v.asset_id "
                            "WHERE v.public_id=:id FOR UPDATE"
                        ),
                        {"id": audio.version_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                fact is None
                or fact["version_status"] not in {"CONFIRMED", "ACTIVE"}
                or fact["asset_id"] != audio.asset_id
                or fact["target_id"] != audio.target_id
                or fact["status"] != "CONFIRMED"
                or fact["asset_type"] != "audio"
                or fact["duration_ms"] != audio.duration_ms
                or not security_status_usable(
                    fact["security_status"], require_review=self.require_review
                )
            ):
                errors.append("AUDIO_RESOURCE_INVALID")
        if content.cover_asset_id:
            fact = (
                (
                    await session.execute(
                        text(
                            "SELECT status,security_status,asset_type "
                            "FROM media_asset WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": content.cover_asset_id},
                    )
                )
                .mappings()
                .first()
            )
            if (
                fact is None
                or fact["status"] != "CONFIRMED"
                or fact["asset_type"] != "images"
                or not security_status_usable(
                    fact["security_status"], require_review=self.require_review
                )
            ):
                errors.append("COVER_RESOURCE_INVALID")
        return errors

    async def command(
        self, action: str, identity: str, actor: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not key or len(key) > 100:
            raise AppError("IDEMPOTENCY_KEY_REQUIRED", "需要有效幂等键", 422)
        scope = f"short-reading:{action}:{identity}"
        request_hash = digest(payload)
        async with self.sessions() as session, session.begin():
            replay = await self.commands.creation_replay(session, scope, actor, key, request_hash)
            if replay is not None:
                return replay
            if action == "create":
                result = await self.create_within(
                    session, payload["series_id"], payload["content"], actor
                )
                identity = result["content_id"]
            else:
                # Lock module before content, matching BFF's final authorization lock order.
                await session.execute(
                    text(
                        "SELECT module_type FROM learning_module_config "
                        "WHERE module_type='short_reading' FOR UPDATE"
                    )
                )
                reading = await self.row(session, identity, True)
                if payload["expected_version"] != reading["version"]:
                    raise AppError("CONTENT_VERSION_CONFLICT", "短文版本已变化", 409)
                result = await self.mutate(session, action, reading, actor, payload)
            now = datetime.now(UTC)
            await session.execute(
                text(
                    "INSERT INTO audit_event(public_id,actor_public_id,"
                    "action,object_type,object_public_id,after_summary,request_id,created_at) "
                    "VALUES(:id,:actor,:action,'short_reading',:object,:summary,:request,:now)"
                ),
                {
                    "id": new_ulid(now),
                    "actor": actor,
                    "action": f"short_reading.{action}",
                    "object": identity,
                    "summary": encode(result),
                    "request": key,
                    "now": now,
                },
            )
            await self.commands.remember_creation(
                session, scope, actor, key, request_hash, result, now
            )
            return result

    async def mutate(
        self,
        session: AsyncSession,
        action: str,
        reading: dict[str, Any],
        actor: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        identity = reading["public_id"]
        revision_id = payload.get("revision_id") or reading["draft_revision_id"]
        if action in {"save", "publish", "revision"}:
            source = payload.get("source_revision_id") if action == "revision" else revision_id
            revision = await self.revision(session, identity, source, True) if source else None
            if action == "revision":
                number = await session.scalar(
                    text(
                        "SELECT COALESCE(MAX(version_no),0)+1 "
                        "FROM short_reading_revision WHERE short_reading_id=:id"
                    ),
                    {"id": identity},
                )
                revision_id = new_ulid(datetime.now(UTC))
                content = revision["content_snapshot"] if revision else self.normalize({})
                await self.insert_revision(
                    session, identity, revision_id, content, actor, int(number or 1), source
                )
                await session.execute(
                    text("UPDATE short_reading SET draft_revision_id=:r WHERE public_id=:id"),
                    {"r": revision_id, "id": identity},
                )
            else:
                if revision is None or revision["status"] != "DRAFT":
                    raise AppError("PUBLISHED_REVISION_IMMUTABLE", "已发布版本不可修改", 409)
                if action == "save":
                    if payload["expected_edit_version"] != revision["edit_version"]:
                        raise AppError("CONTENT_VERSION_CONFLICT", "草稿编辑版本已变化", 409)
                    content = self.normalize(payload["content"])
                    await session.execute(
                        text(
                            "UPDATE short_reading_revision SET "
                            "content_snapshot=:content,content_hash=:hash,"
                            "edit_version=edit_version+1 "
                            "WHERE public_id=:id AND edit_version=:version"
                        ),
                        {
                            "content": encode(content),
                            "hash": digest(content),
                            "id": revision_id,
                            "version": payload["expected_edit_version"],
                        },
                    )
                else:
                    if reading["draft_revision_id"] != revision_id:
                        raise AppError("REVISION_NOT_CURRENT", "只能发布当前短文草稿", 409)
                    if (
                        payload.get("expected_edit_version") is not None
                        and payload["expected_edit_version"] != revision["edit_version"]
                    ):
                        raise AppError("CONTENT_VERSION_CONFLICT", "草稿编辑版本已变化", 409)
                    if payload.get("candidate_hash") is not None and (
                        payload["candidate_hash"] != revision["content_hash"]
                        or payload["candidate_hash"] != digest(revision["content_snapshot"])
                    ):
                        raise AppError("PUBLICATION_PREVIEW_STALE", "冻结短文候选已变化", 409)
                    if not payload.get("manual_confirmed"):
                        raise AppError(
                            "MANUAL_PUBLISH_CONFIRMATION_REQUIRED", "需要人工确认发布", 409
                        )
                    errors = await self.inspect(session, revision)
                    if errors:
                        raise AppError(
                            "PUBLISH_CHECK_FAILED",
                            "短文未通过发布检查",
                            409,
                            {"error_codes": errors},
                        )
                    await session.execute(
                        text(
                            "UPDATE short_reading_revision SET status='SUPERSEDED' "
                            "WHERE short_reading_id=:id AND status='PUBLISHED'"
                        ),
                        {"id": identity},
                    )
                    await session.execute(
                        text(
                            "UPDATE short_reading_revision SET status='PUBLISHED' "
                            "WHERE public_id=:id"
                        ),
                        {"id": revision_id},
                    )
                    await session.execute(
                        text(
                            "UPDATE short_reading SET status='PUBLISHED',"
                            "published_revision_id=:r,draft_revision_id=NULL WHERE public_id=:id"
                        ),
                        {"r": revision_id, "id": identity},
                    )
        elif action == "sort-order":
            sort_order = payload.get("sort_order")
            if type(sort_order) is not int or not -2147483648 <= sort_order <= 2147483647:
                raise AppError("SORT_ORDER_INVALID", "排序值必须为有效整数", 422)
            await session.execute(
                text("UPDATE short_reading SET sort_order=:position WHERE public_id=:id"),
                {"position": sort_order, "id": identity},
            )
        elif action == "offline":
            await session.execute(
                text("UPDATE short_reading SET status='OFFLINE' WHERE public_id=:id"),
                {"id": identity},
            )
        elif action == "trial":
            if reading["status"] != "PUBLISHED" and payload["enabled"]:
                raise AppError("CONTENT_NOT_PUBLISHED", "试读只允许已发布短文", 409)
            await session.execute(
                text(
                    "INSERT INTO short_reading_trial(short_reading_id,enabled,"
                    "version,updated_by,updated_at) VALUES(:id,:enabled,1,:actor,:now) "
                    "ON DUPLICATE KEY UPDATE enabled=:enabled,version=version+1,"
                    "updated_by=:actor,updated_at=:now"
                ),
                {
                    "id": identity,
                    "enabled": payload["enabled"],
                    "actor": actor,
                    "now": datetime.now(UTC),
                },
            )
        else:
            raise AppError("COMMAND_INVALID", "操作无效", 422)
        await session.execute(
            text(
                "UPDATE short_reading SET version=version+1,updated_at=:now "
                "WHERE public_id=:id AND version=:version"
            ),
            {"id": identity, "version": reading["version"], "now": datetime.now(UTC)},
        )
        return {
            "content_id": identity,
            "revision_id": revision_id,
            "version": reading["version"] + 1,
            **({"sort_order": payload["sort_order"]} if action == "sort-order" else {}),
        }

    async def published_detail_within(
        self, session: AsyncSession, identity: str
    ) -> ShortReadingDetail:
        reading = await self.row(session, identity)
        if reading["status"] != "PUBLISHED" or not reading["published_revision_id"]:
            raise AppError("CONTENT_NOT_PUBLISHED", "短文未发布", 404)
        revision = await self.revision(session, identity, reading["published_revision_id"])
        content = ReadingContent.model_validate(revision["content_snapshot"])
        audio = content.audio
        return ShortReadingDetail(
            content_id=identity,
            content_version=revision["version_no"],
            revision_id=revision["public_id"],
            title_en=content.title_en,
            title_zh=content.title_zh,
            category=content.category,
            topic=content.topic,
            level=content.level,
            safe_intro=content.safe_intro,
            paragraphs_en=content.paragraphs_en,
            paragraph_ids=content.paragraph_ids,
            translation_zh=content.translation_zh,
            cover_asset_id=content.cover_asset_id,
            whole_audio=ResourceReference(
                target_id=audio.target_id,
                resource_version=audio.version_id,
                duration_ms=audio.duration_ms,
            )
            if audio
            else None,
        )
