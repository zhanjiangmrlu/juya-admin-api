"""Draft-scoped FULL upload targets; generic private media upload remains reusable."""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content_import.parser import canonical_hash
from juya_admin_api.modules.media.service import MediaService
from juya_admin_api.modules.short_reading.router import AdminDependency
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class TargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_edit_version: int = Field(ge=1)


class BindRequest(TargetRequest):
    target_id: str = Field(min_length=1, max_length=26)
    asset_id: str = Field(min_length=1, max_length=26)


class UploadTargetResponse(BaseModel):
    id: str
    stable_key: str
    target_type: str
    active_version_id: str | None
    content_id: str
    revision_id: str
    version: int
    edit_version: int


class ShortReadingUploads:
    def __init__(self, store: ShortReadingStore, media: MediaService):
        self.store = store
        self.media = media

    @staticmethod
    def stable_key(identity: str, revision_id: str) -> str:
        return f"short-reading:{identity}:{revision_id}:FULL"

    async def draft(
        self, session: AsyncSession, identity: str, revision_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        await session.execute(
            text(
                "SELECT module_type FROM learning_module_config "
                "WHERE module_type='short_reading' FOR UPDATE"
            )
        )
        reading = await self.store.row(session, identity, True)
        if reading["version"] != payload["expected_version"]:
            raise AppError("CONTENT_VERSION_CONFLICT", "短文版本已变化", 409)
        if reading["draft_revision_id"] != revision_id:
            raise AppError("UPLOAD_REVISION_INVALID", "上传只能绑定当前短文草稿", 409)
        revision = await self.store.revision(session, identity, revision_id, True)
        if revision["status"] != "DRAFT":
            raise AppError("PUBLISHED_REVISION_IMMUTABLE", "已发布版本不能绑定上传", 409)
        if revision["edit_version"] != payload["expected_edit_version"]:
            raise AppError("CONTENT_VERSION_CONFLICT", "短文草稿编辑版本已变化", 409)
        return reading, revision

    async def target(
        self, identity: str, revision_id: str, payload: dict[str, Any], actor: str, key: str
    ) -> dict[str, Any]:
        scope = "short-reading:audio-upload:" + identity
        digest = canonical_hash([revision_id, payload])
        async with self.store.sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT module_type FROM learning_module_config "
                    "WHERE module_type='short_reading' FOR UPDATE"
                )
            )
            replay = await self.store.commands.creation_replay(session, scope, actor, key, digest)
            if replay is not None:
                return replay
            reading, revision = await self.draft(session, identity, revision_id, payload)
            stable = self.stable_key(identity, revision_id)
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,target_type) "
                    "VALUES(:id,:stable,'FULL') ON DUPLICATE KEY UPDATE id=id"
                ),
                {"id": new_ulid(datetime.now(UTC)), "stable": stable},
            )
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT t.public_id,t.target_type,v.public_id AS active_version_id "
                            "FROM audio_target t LEFT JOIN audio_version v ON "
                            "v.id=t.active_version_id "
                            "WHERE t.stable_key=:stable FOR UPDATE"
                        ),
                        {"stable": stable},
                    )
                )
                .mappings()
                .one()
            )
            if target["target_type"] != "FULL":
                raise AppError("AUDIO_TARGET_MISMATCH", "短文上传目标角色冲突", 409)
            result = dict(
                id=target["public_id"],
                stable_key=stable,
                target_type="FULL",
                active_version_id=target["active_version_id"],
                content_id=identity,
                revision_id=revision_id,
                version=reading["version"],
                edit_version=revision["edit_version"],
            )
            await self.remember(session, scope, digest, identity, actor, key, result)
            return result

    async def bind(
        self, identity: str, revision_id: str, payload: dict[str, Any], actor: str, key: str
    ) -> dict[str, Any]:
        scope = "short-reading:audio-bind:" + identity
        digest = canonical_hash([revision_id, payload])
        # Replay precedes file I/O and current-version checks, preserving successful receipts.
        async with self.store.sessions() as session, session.begin():
            replay = await self.store.commands.creation_replay(session, scope, actor, key, digest)
            if replay is not None:
                return replay
            await self.draft(session, identity, revision_id, payload)
        asset = await self.media.get_asset(payload["asset_id"])
        if asset.asset_type != "audio" or not asset.duration_ms:
            raise AppError("AUDIO_ASSET_INVALID", "上传素材必须为已解码音频", 422)
        await self.media.read_asset_bytes(asset)
        async with self.store.sessions() as session, session.begin():
            await session.execute(
                text(
                    "SELECT module_type FROM learning_module_config "
                    "WHERE module_type='short_reading' FOR UPDATE"
                )
            )
            replay = await self.store.commands.creation_replay(session, scope, actor, key, digest)
            if replay is not None:
                return replay
            reading, revision = await self.draft(session, identity, revision_id, payload)
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT id,public_id,target_type,stable_key FROM audio_target "
                            "WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": payload["target_id"]},
                    )
                )
                .mappings()
                .first()
            )
            if (
                not target
                or target["target_type"] != "FULL"
                or target["stable_key"] != self.stable_key(identity, revision_id)
            ):
                raise AppError("AUDIO_TARGET_MISMATCH", "音频目标不属于当前短文草稿FULL角色", 409)
            current = (
                (
                    await session.execute(
                        text(
                            "SELECT id,status,security_status,sha256,object_key,duration_ms "
                            "FROM media_asset WHERE public_id=:id FOR UPDATE"
                        ),
                        {"id": asset.id},
                    )
                )
                .mappings()
                .one()
            )
            if (
                current["status"] != "CONFIRMED"
                or not security_status_usable(
                    current["security_status"], require_review=self.store.require_review
                )
                or current["sha256"] != asset.sha256
                or current["object_key"] != asset.object_key
                or current["duration_ms"] != asset.duration_ms
            ):
                raise AppError("MEDIA_ASSET_CHANGED", "绑定期间素材事实已变化", 409)
            existing = await session.scalar(
                text(
                    "SELECT public_id FROM audio_version WHERE "
                    "target_id=:target AND asset_id=:asset "
                    "AND source='MANUAL' AND status IN "
                    "('CANDIDATE','READY','CONFIRMED','ACTIVE','SUPERSEDED') "
                    "ORDER BY version_no DESC LIMIT 1 FOR UPDATE"
                ),
                {"target": target["id"], "asset": current["id"]},
            )
            version = str(existing) if existing else new_ulid(datetime.now(UTC))
            if existing is None:
                number = await session.scalar(
                    text(
                        "SELECT COALESCE(MAX(version_no),0)+1 FROM "
                        "audio_version WHERE target_id=:id"
                    ),
                    {"id": target["id"]},
                )
                await session.execute(
                    text(
                        "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,source,"
                        "status,created_by,created_at) VALUES(:id,:target,:asset,:number,'MANUAL',"
                        "'READY',:actor,UTC_TIMESTAMP(6))"
                    ),
                    {
                        "id": version,
                        "target": target["id"],
                        "asset": current["id"],
                        "number": number,
                        "actor": actor,
                    },
                )
            content = dict(revision["content_snapshot"])
            content["audio"] = dict(
                target_id=target["public_id"],
                version_id=version,
                asset_id=asset.id,
                duration_ms=asset.duration_ms,
                listening_confirmed=False,
                confirmed_asset_id=None,
                confirmed_version_id=None,
            )
            result = await self.store.mutate(
                session,
                "save",
                reading,
                actor,
                dict(
                    expected_version=payload["expected_version"],
                    expected_edit_version=payload["expected_edit_version"],
                    revision_id=revision_id,
                    content=content,
                ),
            )
            result.update(audio=content["audio"])
            await self.remember(session, scope, digest, identity, actor, key, result)
            return result

    async def remember(
        self,
        session: AsyncSession,
        scope: str,
        digest: str,
        identity: str,
        actor: str,
        key: str,
        result: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        await session.execute(
            text(
                "INSERT INTO audit_event(public_id,actor_public_id,"
                "action,object_type,object_public_id,"
                "after_summary,request_id,created_at) "
                "VALUES(:id,:actor,:action,'short_reading',:identity,:summary,:key,:now)"
            ),
            {
                "id": new_ulid(now),
                "actor": actor,
                "action": scope.split(":")[1],
                "identity": identity,
                "summary": encode(result),
                "key": key,
                "now": now,
            },
        )
        await self.store.commands.remember_creation(session, scope, actor, key, digest, result, now)


def create_short_reading_upload_router(
    store: ShortReadingStore,
    media: MediaService,
    *,
    current_admin_write: AdminDependency,
    current_admin: AdminDependency | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/admin/content/short-readings", tags=["short-reading-uploads"]
    )
    service = ShortReadingUploads(store, media)

    @router.post(
        "/{identity}/revisions/{revision_id}/audio-upload-target",
        response_model=UploadTargetResponse,
    )
    async def target(
        identity: str,
        revision_id: str,
        body: TargetRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> JSONResponse:
        result = await service.target(
            identity, revision_id, body.model_dump(), str(admin.admin_user_id), key
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/{identity}/revisions/{revision_id}/audio-upload-target/bind")
    async def bind(
        identity: str,
        revision_id: str,
        body: BindRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)],
    ) -> JSONResponse:
        result = await service.bind(
            identity, revision_id, body.model_dump(), str(admin.admin_user_id), key
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router
