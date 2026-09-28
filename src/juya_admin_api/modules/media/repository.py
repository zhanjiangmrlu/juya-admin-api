from datetime import UTC
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.media.service import MediaAsset


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
