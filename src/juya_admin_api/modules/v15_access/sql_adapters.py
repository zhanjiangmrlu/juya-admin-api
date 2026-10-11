"""SQL adapters over admin-owned content and read-only BFF-owned snapshots."""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_contracts import (
    ConfirmedEntry,
    ContentType,
    EntryType,
    PronunciationReference,
    PublishedSentence,
    ResourceReference,
    SceneDetail,
    SourceLocator,
    content_type,
)
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.modules.v15_access.credentials import details_hash
from juya_admin_api.modules.v15_access.ports import AccessFacts, ContentDetail, FavoriteFacts
from juya_admin_api.shared.errors import AppError


class SQLContentLoader:
    def __init__(self, short_readings: ShortReadingStore) -> None:
        self.short_readings = short_readings

    async def __call__(
        self, session: AsyncSession, kind: ContentType, identity: str
    ) -> ContentDetail:
        if kind == "short_reading":
            return await self.short_readings.published_detail_within(session, identity)
        row = (
            (
                await session.execute(
                    text(
                        "SELECT r.public_id,r.version_no,r.content_snapshot "
                        "FROM scene s JOIN scene_revision r ON r.id=s.published_revision_id "
                        "WHERE s.public_id=:id AND s.status='PUBLISHED' AND r.status='PUBLISHED'"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("CONTENT_NOT_PUBLISHED", "内容未发布", 404)
        content = SceneContent.model_validate(decode(row["content_snapshot"]))
        if content.schema_version != 2 or content_type(content.template_type) != kind:
            raise AppError("CONTENT_SCHEMA_NOT_READY", "内容尚未完成V1.5版本确认", 409)
        groups: dict[str, list[ConfirmedEntry]] = {"word": [], "chunk": []}
        for entries, entry_kind in ((content.vocabulary, "word"), (content.chunks, "chunk")):
            for entry in entries:
                if not entry.confirmed or not all(
                    (entry.entry_id, entry.sense_id, entry.source_id)
                ):
                    raise AppError("CONTENT_SCHEMA_NOT_READY", "本篇条目尚未确认", 409)
                groups[entry_kind].append(
                    ConfirmedEntry(
                        entry_id=entry.entry_id,
                        entry_type=cast(EntryType, entry_kind),
                        sense_id=entry.sense_id,
                        source_id=entry.source_id,
                        entry_version=entry.entry_version,
                        english=entry.english,
                        phonetic=entry.phonetic,
                        meaning_zh=entry.chinese,
                        explanation=entry.explanation,
                        source_sentence=entry.source_sentence,
                        source_locator=SourceLocator.model_validate(entry.source_locator)
                        if entry.source_locator
                        else None,
                        image_asset_id=entry.icon_asset_id,
                        pronunciation=PronunciationReference(
                            target_id=entry.pronunciation_target_id,
                            can_prepare=entry.allow_synthesis,
                            resource_version=entry.audio_version_id,
                        )
                        if entry.pronunciation_target_id
                        else None,
                    )
                )
        return SceneDetail(
            content_id=identity,
            content_type=kind,
            content_version=row["version_no"],
            revision_id=row["public_id"],
            title_en=content.title_en,
            title_zh=content.title_zh,
            words=groups["word"],
            chunks=groups["chunk"],
            dialogue=[
                PublishedSentence(
                    sentence_id=s.id,
                    speaker=s.speaker,
                    english=s.english,
                    chinese=s.chinese,
                    position=i + 1,
                    start_ms=s.start_ms,
                    end_ms=s.end_ms,
                    slice_target_id=s.slice_target_id,
                    slice_version=s.slice_version,
                )
                for i, s in enumerate(content.dialogue)
            ],
            cover_asset_id=content.cover_asset_id,
            learning_image_asset_id=content.original_image_asset_id,
            whole_audio=ResourceReference(
                target_id=content.audio.target_id,
                resource_version=content.audio.version_id,
                duration_ms=content.audio.duration_ms,
            )
            if content.audio
            else None,
        )


class SQLFavoriteAuthority:
    async def __call__(
        self, session: AsyncSession, user_id: str, favorite_id: str
    ) -> FavoriteFacts:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT f.public_id AS favorite_id,f.entry_id,f.sense_id,"
                        "f.source_id,f.entry_type,r.public_id AS snapshot_revision_id,"
                        "r.content_type,r.content_id,"
                        "r.revision_id,r.saved_content_version,r.image_asset_id,r.pronunciation_target_id,"
                        "r.resource_version,r.safe_title_en,r.safe_title_zh FROM user_account u "
                        "JOIN favorite_snapshot f ON f.user_id=u.id "
                        "JOIN favorite_snapshot_revision r "
                        "ON r.public_id=f.current_snapshot_revision_id "
                        "AND r.favorite_id=f.id WHERE u.public_id=:user AND u.status='ACTIVE' "
                        "AND f.public_id=:favorite AND f.active=1 FOR SHARE"
                    ),
                    {"user": user_id, "favorite": favorite_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("FAVORITE_NOT_FOUND", "本人有效收藏不存在", 404)
        return FavoriteFacts(**dict(row))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SQLAuthorizer:
    async def __call__(
        self,
        session: AsyncSession,
        user_id: str,
        kind: ContentType,
        identity: str,
        now: datetime,
        *,
        lock: bool,
    ) -> AccessFacts:
        suffix = " FOR SHARE" if lock else ""
        module = "short_reading" if kind == "short_reading" else "scene_learning"
        enabled = await session.scalar(
            text("SELECT enabled FROM learning_module_config WHERE module_type=:module" + suffix),
            {"module": module},
        )
        config = await session.scalar(
            text("SELECT version FROM discovery_config_state WHERE id=1" + suffix)
        )
        user = (
            (
                await session.execute(
                    text(
                        "SELECT id,status,updated_at FROM user_account WHERE public_id=:id" + suffix
                    ),
                    {"id": user_id},
                )
            )
            .mappings()
            .first()
        )
        module_version = int(config or 0)
        if user is None or user["status"] != "ACTIVE" or not enabled:
            return AccessFacts(False, False, module_version, "DISABLED")
        table = "short_reading" if kind == "short_reading" else "scene"
        row = (
            (
                await session.execute(
                    text(
                        f"SELECT id,status,published_revision_id FROM {table} "
                        "WHERE public_id=:id" + suffix
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["status"] != "PUBLISHED" or row["published_revision_id"] is None:
            return AccessFacts(False, False, module_version, "UNPUBLISHED")
        params = {"user": user["id"], "kind": kind, "identity": identity, "scene": row["id"]}
        package_rows = (
            (
                await session.execute(
                    text(
                        "SELECT p.id,p.status FROM content_package p "
                        "JOIN content_package_item i ON i.package_id=p.id "
                        "WHERE i.content_type=:kind AND i.content_id=:identity ORDER BY p.id"
                        + suffix
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        if kind != "short_reading":
            legacy = (
                (
                    await session.execute(
                        text(
                            "SELECT p.id,p.status FROM content_package p "
                            "JOIN content_package_scene i ON i.package_id=p.id "
                            "WHERE i.scene_id=:scene ORDER BY p.id" + suffix
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            package_rows = [*package_rows, *legacy]
        formal: list[dict[str, Any]] = []
        for package in sorted({p["id"]: p for p in package_rows}.values(), key=lambda p: p["id"]):
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,status,version,expires_at "
                            "FROM formal_entitlement WHERE user_id=:user AND package_id=:package "
                            "ORDER BY id" + suffix
                        ),
                        {**params, "package": package["id"]},
                    )
                )
                .mappings()
                .all()
            )
            if package["status"] == "ACTIVE":
                formal.extend(
                    dict(r)
                    for r in rows
                    if r["status"] == "ACTIVE"
                    and (r["expires_at"] is None or now < utc(r["expires_at"]))
                )
        opened = False
        visible = False
        limited: list[dict[str, Any]] = []
        trial_version = 0
        if kind == "short_reading":
            trial = (
                (
                    await session.execute(
                        text(
                            "SELECT enabled,version FROM short_reading_trial "
                            "WHERE short_reading_id=:identity" + suffix
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            opened = bool(trial and trial["enabled"])
            trial_version = int(trial["version"]) if trial else 0
            visible = (
                True  # Independent short-reading safe preview exists for every published item.
            )
        else:
            current_open = (
                (
                    await session.execute(
                        text(
                            "SELECT id,version FROM open_scene_config "
                            "ORDER BY version DESC LIMIT 1" + suffix
                        )
                    )
                )
                .mappings()
                .first()
            )
            if current_open:
                opened = bool(
                    await session.scalar(
                        text(
                            "SELECT id FROM open_scene_item "
                            "WHERE config_id=:config AND scene_id=:scene" + suffix
                        ),
                        {**params, "config": current_open["id"]},
                    )
                )
                trial_version = int(current_open["version"])
            visible = bool(
                await session.scalar(
                    text("SELECT enabled FROM preview_config WHERE scene_id=:scene" + suffix),
                    params,
                )
            )
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT le.public_id,le.status,le.version,le.expires_at "
                            "FROM limited_entitlement le JOIN limited_campaign_scene lcs "
                            "ON lcs.campaign_version_id=le.campaign_version_id "
                            "WHERE le.user_id=:user AND lcs.scene_id=:scene ORDER BY le.id" + suffix
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            limited = [
                dict(r)
                for r in rows
                if r["status"] == "ACTIVE" and r["expires_at"] and now < utc(r["expires_at"])
            ]
        grants = [*formal, *limited]
        expiries = [utc(r["expires_at"]) for r in grants if r["expires_at"] is not None]
        expires_at = (
            None
            if opened or any(r["expires_at"] is None for r in grants)
            else (max(expiries) if expiries else None)
        )
        full = opened or bool(grants)
        authorization_version = details_hash(
            {
                "user": str(user["updated_at"]),
                "packages": [dict(p) for p in package_rows],
                "trial": trial_version,
                "grants": [{**g, "expires_at": str(g["expires_at"])} for g in grants],
            }
        )
        return AccessFacts(full, full or visible, module_version, authorization_version, expires_at)
