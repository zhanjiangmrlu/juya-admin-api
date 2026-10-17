"""Concrete same-transaction signing for fixed publication and saved favorite references."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.integrations.oss.provider import OssProvider, validate_object_key
from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_contracts import content_type
from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.sql_adapters import SQLAuthorizer, SQLFavoriteAuthority
from juya_admin_api.shared.errors import AppError


class SQLResourceSigner:
    def __init__(
        self,
        oss: OssProvider,
        *,
        require_review: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.oss = oss
        self.require_review = require_review
        self.clock = clock
        self.authorizer = SQLAuthorizer()
        self.favorites = SQLFavoriteAuthority()

    @staticmethod
    def denied() -> AppError:
        return AppError("RESOURCE_REFERENCE_INVALID", "资源不属于当前授权固定引用", 403)

    async def __call__(
        self, session: AsyncSession, request: ResourceRequest, expires_at: datetime
    ) -> dict[str, Any]:
        now = self.clock()
        if request.preview_only and (request.favorite_id is not None or request.role != "COVER"):
            raise self.denied()
        if expires_at.tzinfo is None:
            raise self.denied()
        expires_at = min(expires_at, now + timedelta(seconds=60))
        targets, direct_asset = await self.binding(session, request, now)
        if request.favorite_id is None:
            facts = await self.authorizer(
                session, request.user_id, request.content_type, request.content_id, now, lock=True
            )
            if not (facts.visible if request.preview_only else facts.full_access):
                raise self.denied()
            if facts.expires_at and not request.preview_only:
                expires_at = min(expires_at, facts.expires_at)
        if direct_asset:
            asset = await self.asset(session, direct_asset, "images")
        else:
            asset = await self.audio(session, request, targets)
        metadata = await self.oss.head_object(asset["object_key"])
        if (
            metadata.size != int(asset["size_bytes"])
            or metadata.sha256 != asset["sha256"]
            or metadata.content_type.split(";", 1)[0].lower() != asset["content_type"].lower()
        ):
            raise AppError("MEDIA_ASSET_CHANGED", "固定素材实际对象与登记事实不一致", 409)
        # HEAD and SQL checks consume time; calculate the final short expiry at signing time.
        signed_at = self.clock()
        ttl = min(60, int((expires_at - signed_at).total_seconds()))
        if ttl < 1:
            raise AppError("MEDIA_ACCESS_EXPIRED", "媒体访问权限已到期", 403)
        url = await self.oss.sign_get_url(asset["object_key"], ttl)
        expiry = self.signature_expiry(url, signed_at + timedelta(seconds=ttl))
        if expiry <= self.clock():
            raise AppError("MEDIA_ACCESS_EXPIRED", "媒体签名已到期", 403)
        return {
            "url": url,
            "expires_at": expiry.isoformat(),
            "resource_version": request.resource_version,
            "target_id": request.target_id,
            "asset_id": asset["public_id"],
            "duration_ms": asset.get("duration_ms"),
            "role": request.role,
        }

    async def binding(
        self, session: AsyncSession, request: ResourceRequest, now: datetime
    ) -> tuple[list[str], str | None]:
        if request.preview_only and (request.favorite_id is not None or request.role != "COVER"):
            raise self.denied()
        if request.favorite_id:
            saved = await self.favorites(session, request.user_id, request.favorite_id)
            if (
                saved.content_type != request.content_type
                or saved.content_id != request.content_id
                or saved.revision_id != request.revision_id
                or saved.revision_id is None
                or saved.saved_content_version is None
            ):
                raise self.denied()
            if request.role == "WORD_IMAGE":
                if (
                    saved.entry_type != "word"
                    or request.target_id != saved.image_asset_id
                    or request.asset_id not in {None, saved.image_asset_id}
                    or request.resource_version is not None
                ):
                    raise self.denied()
                return [], saved.image_asset_id
            if (
                request.role != ("WORD" if saved.entry_type == "word" else "CHUNK")
                or not saved.pronunciation_target_id
                or (
                    saved.resource_version is not None
                    and saved.resource_version != request.resource_version
                )
            ):
                raise self.denied()
            return [saved.pronunciation_target_id], None
        # Full-content path rechecks live access before touching publication references.
        facts = await self.authorizer(
            session, request.user_id, request.content_type, request.content_id, now, lock=True
        )
        if not (facts.visible if request.preview_only else facts.full_access):
            raise self.denied()
        if request.content_type == "short_reading":
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT r.content_snapshot FROM short_reading s "
                            "JOIN short_reading_revision r ON r.public_id=s.published_revision_id "
                            "WHERE s.public_id=:content AND s.status='PUBLISHED' "
                            "AND r.public_id=:revision "
                            "AND r.status='PUBLISHED' FOR SHARE"
                        ),
                        {"content": request.content_id, "revision": request.revision_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise self.denied()
            reading = ReadingContent.model_validate(decode(row["content_snapshot"]))
            if (
                request.role == "COVER"
                and request.target_id == reading.cover_asset_id
                and request.resource_version is None
            ):
                return [], reading.cover_asset_id
            if (
                request.role == "WHOLE"
                and reading.audio
                and request.target_id == reading.audio.target_id
                and request.resource_version == reading.audio.version_id
            ):
                return [], None
            raise self.denied()
        row = (
            (
                await session.execute(
                    text(
                        "SELECT r.content_snapshot FROM scene s "
                        "JOIN scene_revision r ON r.id=s.published_revision_id "
                        "WHERE s.public_id=:content AND s.status='PUBLISHED' "
                        "AND r.public_id=:revision "
                        "AND r.status='PUBLISHED' FOR SHARE"
                    ),
                    {"content": request.content_id, "revision": request.revision_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise self.denied()
        content = SceneContent.model_validate(decode(row["content_snapshot"]))
        if (
            content.schema_version != 2
            or content_type(content.template_type) != request.content_type
        ):
            raise self.denied()
        if request.role in {"COVER", "LEARNING_IMAGE"}:
            asset = (
                content.cover_asset_id
                if request.role == "COVER"
                else content.original_image_asset_id
            )
            if request.target_id == asset and request.resource_version is None:
                return [], asset
        if request.role == "WORD_IMAGE":
            assets = {
                e.icon_asset_id for e in content.vocabulary if e.confirmed and e.icon_asset_id
            }
            if request.target_id in assets and request.resource_version is None:
                return [], request.target_id
        if (
            request.role == "WHOLE"
            and content.audio
            and request.target_id == content.audio.target_id
            and request.resource_version == content.audio.version_id
        ):
            return [], None
        if request.role == "SENTENCE":
            matched = [
                s
                for s in content.dialogue
                if s.slice_target_id == request.target_id
                and s.slice_version == request.resource_version
            ]
            if len(matched) == 1:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT status,confirmed_by FROM sentence_audio_slice "
                                "WHERE revision_id=:revision AND sentence_id=:sentence "
                                "AND target_id=:target "
                                "AND resource_version=:version FOR SHARE"
                            ),
                            {
                                "revision": request.revision_id,
                                "sentence": matched[0].id,
                                "target": request.target_id,
                                "version": request.resource_version,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if row and row["status"] == "CONFIRMED" and row["confirmed_by"]:
                    return [], None
        if request.role in {"WORD", "CHUNK"}:
            entries = content.vocabulary if request.role == "WORD" else content.chunks
            targets = [
                e.pronunciation_target_id
                for e in entries
                if e.confirmed and e.pronunciation_target_id
            ]
            if targets:
                return targets, None
        raise self.denied()

    async def asset(self, session: AsyncSession, identity: str, kind: str) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,object_key,asset_type,content_type,"
                        "size_bytes,sha256,status,security_status,duration_ms,width,height "
                        "FROM media_asset WHERE public_id=:id FOR SHARE"
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("RESOURCE_NOT_READY", "固定媒体尚未登记", 403)
        asset = dict(row)
        self.validate_asset(asset, kind)
        return asset

    def validate_asset(self, asset: dict[str, Any], kind: str) -> None:
        if (
            asset["status"] != "CONFIRMED"
            or asset["asset_type"] != kind
            or not security_status_usable(
                asset["security_status"], require_review=self.require_review
            )
            or not asset["object_key"].startswith("sealed/media/" + kind + "/")
            or int(asset["size_bytes"]) <= 0
            or int(asset["size_bytes"]) > (50 if kind == "audio" else 20) * 1024 * 1024
            or asset["content_type"].lower()
            not in (
                {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/aac"}
                if kind == "audio"
                else {"image/png", "image/jpeg", "image/webp"}
            )
            or len(asset["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in asset["sha256"].lower())
            or (kind == "audio" and int(asset.get("duration_ms") or 0) <= 0)
            or (
                kind == "images"
                and (int(asset.get("width") or 0) <= 0 or int(asset.get("height") or 0) <= 0)
            )
        ):
            raise AppError("RESOURCE_NOT_READY", "固定媒体未通过实际检查或审核", 403)
        validate_object_key(asset["object_key"])

    async def audio(
        self, session: AsyncSession, request: ResourceRequest, allowed_pronunciations: list[str]
    ) -> dict[str, Any]:
        if not request.resource_version:
            raise self.denied()
        targets: list[Target] = []
        for identity in allowed_pronunciations:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM pronunciation_target WHERE public_id=:id FOR SHARE"),
                        {"id": identity},
                    )
                )
                .mappings()
                .first()
            )
            if row and row["status"] == "CONFIRMED" and row["target_type"] == request.role:
                targets.append(Target(**{name: row[name] for name in Target.__dataclass_fields__}))
        version = (
            (
                await session.execute(
                    text(
                        "SELECT v.status,t.public_id AS target_id,t.target_type,"
                        "m.public_id AS asset_id FROM audio_version v "
                        "JOIN audio_target t ON t.id=v.target_id "
                        "JOIN media_asset m ON m.id=v.asset_id WHERE v.public_id=:version FOR SHARE"
                    ),
                    {"version": request.resource_version},
                )
            )
            .mappings()
            .first()
        )
        if version:
            valid = version["target_id"] == request.target_id
            if request.role in {"WORD", "CHUNK"}:
                valid = any(
                    t.uploaded_target_id == version["target_id"]
                    and (
                        t.uploaded_resource_version == request.resource_version
                        or request.favorite_id is not None
                    )
                    and request.target_id in {t.public_id, t.uploaded_target_id}
                    for t in targets
                )
                if not targets and request.favorite_id is not None:
                    valid = (
                        request.target_id in allowed_pronunciations
                        and version["target_id"] == request.target_id
                    )
            types = {
                "WHOLE": {
                    "WHOLE",
                    "FULL",
                    "DIALOGUE",
                    "dialogue",
                    "SHORT_READING",
                    "short_reading",
                    "whole",
                    "scene",
                    "SCENE",
                },
                "SENTENCE": {"SENTENCE", "sentence"},
                "WORD": {"WORD", "word", "VOCABULARY", "vocabulary"},
                "CHUNK": {"CHUNK", "chunk", "PHRASE", "phrase"},
            }
            if (
                not valid
                or version["target_type"] not in types.get(request.role, set())
                or version["status"] not in {"ACTIVE", "CONFIRMED", "SUPERSEDED"}
            ):
                raise self.denied()
            return await self.asset(session, version["asset_id"], "audio")
        if request.role not in {"WORD", "CHUNK"} or not targets:
            raise self.denied()
        cached = (
            (
                await session.execute(
                    text(
                        "SELECT c.cache_key,c.object_key,c.duration_ms,"
                        "c.resource_version,t.status,t.target_snapshot,t.config,t.target_id "
                        "FROM pronunciation_cache c JOIN pronunciation_task t "
                        "ON t.public_id=c.resource_version "
                        "WHERE c.resource_version=:version AND t.cache_key=c.cache_key "
                        "FOR SHARE"
                    ),
                    {"key": request.target_id, "version": request.resource_version},
                )
            )
            .mappings()
            .first()
        )
        if cached is None or cached["status"] != "READY" or int(cached["duration_ms"]) <= 0:
            raise AppError("RESOURCE_NOT_READY", "词语缓存尚未就绪", 403)
        config = Config(**decode(cached["config"]))
        frozen = Target(**decode(cached["target_snapshot"]))
        if (
            frozen.public_id != cached["target_id"]
            or cache_key(frozen, config) != cached["cache_key"]
        ):
            raise self.denied()
        if not any(
            cache_key(target, config) == cached["cache_key"]
            and request.target_id in {target.public_id, cached["cache_key"]}
            for target in targets
        ):
            raise self.denied()
        identity = await session.scalar(
            text("SELECT public_id FROM media_asset WHERE object_key=:key FOR SHARE"),
            {"key": cached["object_key"]},
        )
        if identity is None:
            raise AppError("RESOURCE_NOT_READY", "缓存音频缺少固定媒体审核登记", 403)
        asset = await self.asset(session, str(identity), "audio")
        if asset["duration_ms"] != cached["duration_ms"]:
            raise self.denied()
        return asset

    @staticmethod
    def signature_expiry(url: str, expected: datetime) -> datetime:
        query = dict(parse_qsl(urlsplit(url).query))
        try:
            if "x-oss-date" in query and "x-oss-expires" in query:
                signed = datetime.strptime(query["x-oss-date"], "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC
                )
                actual = signed + timedelta(seconds=int(query["x-oss-expires"]))
                if actual > expected:
                    raise ValueError("provider exceeded expiry")
                return min(expected, actual)
            if "Expires" in query:
                actual = datetime.fromtimestamp(int(query["Expires"]), tz=UTC)
                if actual > expected:
                    raise ValueError("provider exceeded expiry")
                return min(expected, actual)
        except (ValueError, OverflowError) as error:
            raise AppError("OSS_SIGNATURE_INVALID", "OSS实际签名期限无效", 503) from error
        return expected
