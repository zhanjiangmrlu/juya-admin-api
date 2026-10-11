from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.audio_pipeline.service import SavedSlice, SliceInput
from juya_admin_api.modules.audio_pipeline.slicing import SliceResult
from juya_admin_api.shared.ids import new_ulid

# The content owner provides extraction and locking from the canonical candidate revision.
CurrentInput = Callable[[str, str], Awaitable[SliceInput]]
LockedInput = Callable[[AsyncSession, str, str], Awaitable[SliceInput]]


class SQLAudioRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        current_input: CurrentInput,
        locked_input: LockedInput,
        encoding_config: dict[str, object],
    ) -> None:
        self.sessions = sessions
        self.current_input = current_input
        self.locked_input = locked_input
        self.encoding_config = encoding_config

    async def current(self, revision_id: str, sentence_id: str) -> SliceInput:
        return await self.current_input(revision_id, sentence_id)

    async def find_ready(self, item: SliceInput) -> SavedSlice | None:
        async with self.sessions() as session:
            return await self._find(session, item)

    async def _find(self, session: AsyncSession, item: SliceInput) -> SavedSlice | None:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,asset_id,target_id,resource_version,"
                        "dependency_hash,status,"
                        "actual_duration_ms FROM sentence_audio_slice WHERE revision_id=:revision "
                        "AND sentence_id=:sentence AND dependency_hash=:hash "
                        "AND status IN ('READY','CONFIRMED')"
                    ),
                    {
                        "revision": item.revision_id,
                        "sentence": item.sentence_id,
                        "hash": item.dependency(self.encoding_config),
                    },
                )
            )
            .mappings()
            .first()
        )
        return (
            SavedSlice(
                str(row["public_id"]),
                str(row["asset_id"]),
                str(row["target_id"]),
                str(row["resource_version"]),
                str(row["dependency_hash"]),
                str(row["status"]),
                int(row["actual_duration_ms"]),
            )
            if row
            else None
        )

    async def commit_if_current(
        self, item: SliceInput, result: SliceResult, object_key: str
    ) -> SavedSlice | None:
        import json

        async with self.sessions() as session, session.begin():
            current = await self.locked_input(session, item.revision_id, item.sentence_id)
            if current != item or not current.verified_by:
                return None
            existing = await self._find(session, item)
            if existing:
                return existing
            asset, target, version, slice_id = (new_ulid(datetime.now(UTC)) for _ in range(4))
            now = datetime.now(UTC).replace(tzinfo=None)
            await session.execute(
                text(
                    "INSERT INTO media_asset"
                    " (public_id,object_key,asset_type,content_type,size_bytes,"
                    "sha256,status,security_status,created_by,created_at,duration_ms) "
                    "VALUES (:id,:key,'audio',:mime,:size,:sha,'READY','PENDING',"
                    ":admin,:now,:duration) "
                    "ON DUPLICATE KEY UPDATE id=id"
                ),
                {
                    "id": asset,
                    "key": object_key,
                    "mime": result.content_type,
                    "size": len(result.data),
                    "sha": result.sha256,
                    "admin": item.verified_by,
                    "now": now,
                    "duration": result.duration_ms,
                },
            )
            actual_asset = await session.scalar(
                text("SELECT public_id FROM media_asset WHERE asset_type='audio' AND sha256=:sha"),
                {"sha": result.sha256},
            )
            if actual_asset is None:
                raise ValueError("slice asset registration failed")
            asset = str(actual_asset)
            await session.execute(
                text(
                    "INSERT INTO audio_target (public_id,stable_key,target_type,active_version_id) "
                    "VALUES (:id,:key,'sentence',NULL)"
                ),
                {"id": target, "key": f"slice:{slice_id}", "version": version},
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version"
                    " (public_id,target_id,asset_id,version_no,source,status,"
                    "created_by,created_at) VALUES (:id,"
                    "(SELECT id FROM audio_target WHERE public_id=:target),"
                    "(SELECT id FROM media_asset WHERE public_id=:asset),"
                    "1,'MANUAL','READY',:admin,:now)"
                ),
                {
                    "id": version,
                    "target": target,
                    "asset": asset,
                    "admin": item.verified_by,
                    "now": now,
                },
            )
            await session.execute(
                text(
                    "UPDATE audio_target SET active_version_id="
                    "(SELECT id FROM audio_version WHERE public_id=:version) WHERE"
                    " public_id=:target"
                ),
                {"version": version, "target": target},
            )
            digest = item.dependency(self.encoding_config)
            await session.execute(
                text(
                    "INSERT INTO sentence_audio_slice"
                    " (public_id,content_type,content_id,revision_id,"
                    "sentence_id,source_audio_version,source_sha256,start_ms,end_ms,target_id,"
                    "resource_version,asset_id,dependency_hash,status,"
                    "actual_duration_ms,slice_sha256,"
                    "byte_size,encoding_config,created_at) VALUES (:id,:type,:content,:revision,"
                    ":sentence,:source_version,:source_sha,:start,:end,:target,:version,:asset,"
                    ":hash,'READY',:duration,:sha,:size,:encoding,:now)"
                ),
                {
                    "id": slice_id,
                    "type": item.content_type,
                    "content": item.content_id,
                    "revision": item.revision_id,
                    "sentence": item.sentence_id,
                    "source_version": item.source_audio_version,
                    "source_sha": item.source_sha256,
                    "start": item.boundary.start_ms,
                    "end": item.boundary.end_ms,
                    "target": target,
                    "version": version,
                    "asset": asset,
                    "hash": digest,
                    "duration": result.duration_ms,
                    "sha": result.sha256,
                    "size": len(result.data),
                    "encoding": json.dumps(self.encoding_config),
                    "now": now,
                },
            )
            return SavedSlice(slice_id, asset, target, version, digest, "READY", result.duration_ms)

    async def confirm(
        self, revision_id: str, sentence_id: str, digest: str, reviewer_id: str
    ) -> SavedSlice:
        if not reviewer_id:
            raise ValueError("listening reviewer required")
        async with self.sessions() as session, session.begin():
            current = await self.locked_input(session, revision_id, sentence_id)
            if current.dependency(self.encoding_config) != digest or not current.verified_by:
                raise ValueError("audio dependencies changed before confirmation")
            saved = await self._find(session, current)
            if saved is None:
                raise ValueError("sentence slice not ready")
            await session.execute(
                text(
                    "UPDATE sentence_audio_slice SET status='CONFIRMED',confirmed_by=:reviewer,"
                    "confirmed_at=:now WHERE public_id=:id AND dependency_hash=:hash"
                ),
                {
                    "reviewer": reviewer_id,
                    "now": datetime.now(UTC).replace(tzinfo=None),
                    "id": saved.id,
                    "hash": digest,
                },
            )
            return SavedSlice(
                saved.id,
                saved.asset_id,
                saved.target_id,
                saved.resource_version,
                digest,
                "CONFIRMED",
                saved.actual_duration_ms,
            )
