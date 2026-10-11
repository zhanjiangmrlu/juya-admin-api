"""Explicit V1.5 service assembly shared by HTTP and independently dispatched workers."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.asr.aliyun import AliyunASR
from juya_admin_api.integrations.content_ai.aliyun import AliyunContentAI
from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.integrations.content_security.protocol import ContentSecurityProvider
from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content.v15_materials import LegacyMaterialService
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.media.repository import SQLAlchemyMediaRepository
from juya_admin_api.modules.media.service import MediaAsset, MediaService
from juya_admin_api.modules.pronunciation.domain import Config
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.modules.pronunciation.service import PronunciationService
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.resources import SQLResourceSigner
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.modules.v15_access.sql_adapters import (
    SQLAuthorizer,
    SQLContentLoader,
    SQLFavoriteAuthority,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def tts_config(settings: Settings) -> Config:
    configured = bool(settings.tts_secret_id and settings.tts_secret_key)
    return Config(
        enabled=settings.tts_enabled and configured,
        voice=settings.tts_voice,
        region=settings.tts_region,
        version=str(settings.tts_config_version),
        price_version=settings.tts_price_version,
        microcost_per_character=settings.tts_microcost_per_character,
    )


@dataclass
class V15Runtime:
    proofreading: ProofreadingStore
    readings: ShortReadingStore
    production: ProductionStore
    imports: ImportService
    authoring: SceneAuthoringStore
    access: V15AccessService
    pronunciation: PronunciationService
    candidates: CandidateService
    oss: OssProvider
    client: httpx.AsyncClient
    materials: LegacyMaterialService | None = None


def build_v15_services(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    oss: OssProvider,
    media: MediaService,
    security: ContentSecurityProvider,
) -> V15Runtime:
    proofreading = ProofreadingStore(sessions)
    readings = ShortReadingStore(
        sessions,
        check_confirmation=proofreading.is_confirmed_within,
        require_review=settings.content_security_enabled,
    )
    production = ProductionStore(
        sessions,
        prepare_asset=media.get_asset,
        check_confirmation=proofreading.is_confirmed_within,
        require_review=settings.content_security_enabled,
    )
    stages = StageStore(sessions)
    media_repository = SQLAlchemyMediaRepository(sessions)

    async def bind_media(data: bytes, kind: str, actor: str, role: str) -> dict[str, Any]:
        inspected = await inspect_media(data, kind, settings.ffprobe_path)
        if kind == "images" and inspected.content_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            raise AppError("IMPORT_MEDIA_TYPE_INVALID", "导入图片实际格式不符合要求", 422)
        existing = await media_repository.get_by_hash(kind, inspected.sha256)
        if existing:
            asset = existing
            if not security_status_usable(
                asset.security_status, require_review=settings.content_security_enabled
            ) and asset.security_status not in {"BLOCKED", "REJECTED"}:
                scan = (
                    await security.scan_image(asset.object_key)
                    if kind == "images"
                    else await security.poll_audio(asset.security_request_id)
                    if asset.security_request_id
                    else await security.scan_audio(asset.object_key)
                )
                ready = security_status_usable(
                    scan.status, require_review=settings.content_security_enabled
                )
                asset = await media_repository.update_security(
                    replace(
                        asset,
                        security_status=scan.status,
                        security_request_id=scan.provider_request_id or asset.security_request_id,
                        status="CONFIRMED" if ready else "PENDING",
                    )
                )
        else:
            key = await oss.freeze_bytes(data, kind, inspected.content_type)
            scan = (
                await security.scan_image(key)
                if kind == "images"
                else await security.scan_audio(key)
            )
            ready = security_status_usable(
                scan.status, require_review=settings.content_security_enabled
            )
            asset = await media_repository.save(
                MediaAsset(
                    new_ulid(datetime.now(UTC)),
                    key,
                    kind,
                    inspected.content_type,
                    inspected.size,
                    inspected.sha256,
                    "CONFIRMED" if ready else "PENDING",
                    scan.status,
                    actor,
                    datetime.now(UTC),
                    inspected.width,
                    inspected.height,
                    inspected.duration_ms,
                    scan.provider_request_id or None,
                )
            )
        if not security_status_usable(
            asset.security_status, require_review=settings.content_security_enabled
        ):
            raise AppError("IMPORT_MEDIA_REVIEW_PENDING", "素材尚未通过审核,保留后可局部重试", 409)
        if kind == "images":
            return {"asset_id": asset.id}
        # Create a fixed candidate without claiming a human has listened to it.
        async with sessions() as session, session.begin():
            target, version = new_ulid(datetime.now(UTC)), new_ulid(datetime.now(UTC))
            target_type = {
                "WORD_AUDIO": "WORD",
                "CHUNK_AUDIO": "CHUNK",
                "WHOLE_AUDIO": "WHOLE",
                "FULL_AUDIO": "FULL",
            }.get(role)
            if target_type is None:
                raise AppError("IMPORT_AUDIO_ROLE_INVALID", "音频角色必须明确绑定", 422)
            stable = "v15-import:" + target_type + ":" + asset.sha256
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable_key,target_type) "
                    "VALUES(:id,:stable,:type) "
                    "ON DUPLICATE KEY UPDATE id=id"
                ),
                {"id": target, "stable": stable, "type": target_type},
            )
            row = (
                await session.execute(
                    text(
                        "SELECT id,public_id FROM audio_target WHERE stable_key=:stable FOR UPDATE"
                    ),
                    {"stable": stable},
                )
            ).first()
            assert row
            previous = (
                await session.execute(
                    text(
                        "SELECT v.public_id FROM audio_version v JOIN media_asset m ON "
                        "m.id=v.asset_id "
                        "WHERE v.target_id=:target AND m.public_id=:asset ORDER BY "
                        "v.version_no DESC LIMIT 1"
                    ),
                    {"target": row.id, "asset": asset.id},
                )
            ).first()
            if previous:
                version = previous.public_id
            else:
                await session.execute(
                    text(
                        "INSERT INTO audio_version(public_id,target_id,asset_id,version_no,source,"
                        "status,created_by,created_at) "
                        "SELECT :version,:target,id,1,'MANUAL','READY',:actor,UTC_TIMESTAMP(6) "
                        "FROM media_asset WHERE public_id=:asset"
                    ),
                    {"version": version, "target": row.id, "asset": asset.id, "actor": actor},
                )
            return {
                "target_id": row.public_id,
                "version_id": version,
                "asset_id": asset.id,
                "duration_ms": asset.duration_ms,
            }

    async def prepare_slice(asset_id: str, resource_version: str) -> None:
        asset = await media_repository.get(asset_id)
        if asset is None or not asset.object_key.startswith("sealed/media/"):
            raise AppError("SLICE_MEDIA_MISSING", "切片固定资源缺失", 409)
        data = await oss.read_bytes(asset.object_key, 50 * 1024 * 1024)
        inspected = await inspect_media(data, "audio", settings.ffprobe_path)
        if (inspected.sha256, inspected.duration_ms) != (asset.sha256, asset.duration_ms):
            raise AppError("SLICE_MEDIA_CHANGED", "句子资源字节与登记不一致", 409)
        if asset.status == "CONFIRMED" and asset.security_status == "PASSED":
            return
        scan = (
            await security.poll_audio(asset.security_request_id)
            if asset.security_request_id
            else await security.scan_audio(asset.object_key)
        )
        if scan.status in {"PENDING", "PROCESSING"}:
            scan = await security.poll_audio(scan.provider_request_id)
        await media_repository.update_security(
            replace(
                asset,
                security_status=scan.status,
                security_request_id=scan.provider_request_id or asset.security_request_id,
                status="CONFIRMED" if scan.status == "PASSED" else "PENDING",
            )
        )
        # Manual slice confirmation checks PASSED specifically; disabled review remains pending.
        if scan.status != "PASSED":
            raise AppError("SLICE_REVIEW_PENDING", "句子切片安全审核未通过", 409)
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE media_asset SET "
                    "status='CONFIRMED',security_status='PASSED',security_request_id=:request "
                    "WHERE public_id=:id"
                ),
                {"id": asset_id, "request": scan.provider_request_id},
            )

    async def sign_slice(asset_id: str, ttl: int) -> str:
        asset = await media.get_asset(asset_id)
        return (await media.sign_media(asset.object_key, None, datetime.now(UTC))).url

    authoring = SceneAuthoringStore(
        sessions, proofreading, stages, prepare_slice=prepare_slice, sign_slice=sign_slice
    )
    imports = ImportService(
        sessions, Path(settings.import_storage_path), production, readings, bind_media
    )
    materials = LegacyMaterialService(imports, readings, proofreading.invalidate_within)
    loader, authorizer, favorites = (
        SQLContentLoader(readings),
        SQLAuthorizer(),
        SQLFavoriteAuthority(),
    )
    signer = SQLResourceSigner(oss, require_review=settings.content_security_enabled)
    secret = settings.internal_hmac_secret
    if secret is None:
        raise RuntimeError("JUYA_INTERNAL_HMAC_SECRET required")
    access = V15AccessService(
        sessions,
        content_loader=loader,
        authorizer=authorizer,
        resource_signer=signer,
        favorite_authority=favorites,
        credential_secret=secret.get_secret_value().encode(),
        ttl_seconds=settings.signed_url_ttl_seconds,
    )

    async def authorize_pronunciation(target_id: str, context: dict[str, Any]) -> bool:
        try:
            async with sessions() as session, session.begin():
                if context.get("purpose") == "FAVORITE_REVIEW":
                    saved = await favorites(session, context["user_id"], context["favorite_id"])
                    valid = saved.pronunciation_target_id == target_id
                else:
                    kind, identity = context["content_type"], context["content_id"]
                    facts = await authorizer(
                        session, context["user_id"], kind, identity, datetime.now(UTC), lock=True
                    )
                    if not facts.full_access:
                        return False
                    detail = await loader(session, kind, identity)
                    entries = [*getattr(detail, "words", []), *getattr(detail, "chunks", [])]
                    valid = int(context["content_version"]) == detail.content_version and any(
                        e.entry_id == context["entry_id"]
                        and e.pronunciation
                        and e.pronunciation.target_id == target_id
                        for e in entries
                    )
                if valid:
                    context["authorized_target_id"] = target_id
                return valid
        except (AppError, KeyError, TypeError, ValueError):
            return False

    async def resolve_pronunciation(
        resource_id: str, version: str | None, context: dict[str, Any]
    ) -> dict[str, Any]:
        target = context["authorized_target_id"]
        async with sessions() as session, session.begin():
            if context["purpose"] == "FAVORITE_REVIEW":
                saved = await favorites(session, context["user_id"], context["favorite_id"])
                if saved.pronunciation_target_id != target or saved.revision_id is None:
                    raise AppError("PRONUNCIATION_FORBIDDEN", "收藏发音标识不匹配", 403)
                request = ResourceRequest(
                    context["user_id"],
                    saved.content_type,
                    saved.content_id,
                    saved.revision_id,
                    target,
                    version,
                    "WORD" if saved.entry_type == "word" else "CHUNK",
                    favorite_id=saved.favorite_id,
                )
                expiry = access.expiry(datetime.now(UTC))
            else:
                detail, facts = await access.authorized_within(
                    session,
                    context["user_id"],
                    context["content_type"],
                    context["content_id"],
                    datetime.now(UTC),
                    lock=True,
                )
                entry = next(
                    (
                        e
                        for e in [*getattr(detail, "words", []), *getattr(detail, "chunks", [])]
                        if e.entry_id == context["entry_id"]
                        and e.pronunciation
                        and e.pronunciation.target_id == target
                    ),
                    None,
                )
                if entry is None or int(context["content_version"]) != detail.content_version:
                    raise AppError("PRONUNCIATION_FORBIDDEN", "当前内容发音引用已变化", 403)
                request = ResourceRequest(
                    context["user_id"],
                    detail.content_type,
                    detail.content_id,
                    detail.revision_id,
                    target,
                    version,
                    "WORD" if entry.entry_type == "word" else "CHUNK",
                )
                expiry = access.expiry(datetime.now(UTC), facts)
            return await signer(session, request, expiry)

    pronunciation = PronunciationService(
        SqlPronunciationRepository(sessions),
        tts_config(settings),
        authorize_pronunciation,
        resolve_pronunciation,
    )
    client = httpx.AsyncClient()
    ai = (
        AliyunContentAI(
            client,
            settings.content_ai_api_key.get_secret_value(),
            model=settings.content_ai_model,
            config_version=settings.content_ai_config_version,
        )
        if settings.content_ai_enabled and settings.content_ai_api_key
        else None
    )
    asr = (
        AliyunASR(
            client,
            settings.asr_api_key.get_secret_value(),
            model=settings.asr_model,
            audio_hosts=tuple(h.strip() for h in settings.asr_audio_hosts.split(",") if h.strip()),
        )
        if settings.asr_enabled and settings.asr_api_key and settings.asr_audio_hosts
        else None
    )
    candidates = CandidateService(
        sessions,
        production,
        stages,
        ai=ai,
        asr=asr,
        media=media,
        config_version=settings.content_ai_config_version + ":" + settings.asr_config_version,
        content_ai_daily_token_limit=settings.content_ai_daily_token_limit,
        content_ai_max_output_tokens=settings.content_ai_max_output_tokens,
        asr_daily_seconds_limit=settings.asr_daily_seconds_limit,
    )
    return V15Runtime(
        proofreading,
        readings,
        production,
        imports,
        authoring,
        access,
        pronunciation,
        candidates,
        oss,
        client,
        materials=materials,
    )
