from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.access_policy.service import AccessPolicyService
from juya_admin_api.modules.media.service import MediaAsset
from juya_admin_api.shared.errors import AppError


class SQLAlchemyMediaRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT public_id, object_key, asset_type, content_type, "
                        "size_bytes, sha256, status, security_status, created_by, created_at "
                        "FROM media_asset WHERE asset_type = :asset_type AND sha256 = :sha256"
                    ),
                    {"asset_type": asset_type, "sha256": sha256},
                )
            ).first()
        return None if row is None else _from_row(row)

    async def save(self, asset: MediaAsset) -> MediaAsset:
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO media_asset "
                        "(public_id, object_key, asset_type, content_type, size_bytes, sha256, "
                        "status, security_status, created_by, created_at) VALUES "
                        "(:public_id, :object_key, :asset_type, :content_type, :size_bytes, "
                        ":sha256, :status, :security_status, :created_by, :created_at)"
                    ),
                    {
                        "public_id": asset.id,
                        "object_key": asset.object_key,
                        "asset_type": asset.asset_type,
                        "content_type": asset.content_type,
                        "size_bytes": asset.size,
                        "sha256": asset.sha256,
                        "status": asset.status,
                        "security_status": asset.security_status,
                        "created_by": asset.created_by,
                        "created_at": asset.created_at,
                    },
                )
        except IntegrityError:
            existing = await self.get_by_hash(asset.asset_type, asset.sha256)
            if existing is None:
                raise
            return existing
        return asset


def _from_row(row: Any) -> MediaAsset:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return MediaAsset(
        id=row.public_id,
        object_key=row.object_key,
        asset_type=row.asset_type,
        content_type=row.content_type,
        size=row.size_bytes,
        sha256=row.sha256,
        status=row.status,
        security_status=row.security_status,
        created_by=row.created_by,
        created_at=created_at,
    )


class SQLAlchemySignedTargetResolver:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        access_policy: AccessPolicyService,
    ) -> None:
        self._session_factory = session_factory
        self._access_policy = access_policy

    async def __call__(
        self, target_id: str, user_id: str, now: datetime
    ) -> tuple[str, datetime | None]:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT a.object_key, COALESCE(sentence_scene.public_id, "
                        "entry_scene.public_id) AS scene_public_id FROM audio_target t "
                        "JOIN audio_version v ON v.id = t.active_version_id "
                        "JOIN media_asset a ON a.id = v.asset_id "
                        "LEFT JOIN scene_dialogue_sentence ds ON ds.stable_id = t.stable_key "
                        "LEFT JOIN scene_revision sentence_revision "
                        "ON sentence_revision.id = ds.revision_id "
                        "LEFT JOIN scene sentence_scene "
                        "ON sentence_scene.published_revision_id = sentence_revision.id "
                        "LEFT JOIN scene_entry e ON e.stable_id = t.stable_key "
                        "LEFT JOIN scene_revision entry_revision ON entry_revision.id = "
                        "e.revision_id "
                        "LEFT JOIN scene entry_scene "
                        "ON entry_scene.published_revision_id = entry_revision.id "
                        "WHERE t.public_id = :target_id AND v.status = 'ACTIVE' LIMIT 1"
                    ),
                    {"target_id": target_id},
                )
            ).first()
        if row is None or row.scene_public_id is None:
            raise AppError("MEDIA_TARGET_NOT_FOUND", "媒体目标不存在", 404)
        decision = await self._access_policy.authorize(user_id, row.scene_public_id, now)
        if not decision.has_full_access:
            raise AppError("MEDIA_ACCESS_DENIED", "无权访问该媒体", 403)
        return row.object_key, decision.earliest_expires_at
