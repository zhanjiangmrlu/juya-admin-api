import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from juya_admin_api.integrations.oss.provider import ObjectMetadata, OssProvider, UploadPolicy
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

IMAGE_MAX_BYTES = 20 * 1024 * 1024
AUDIO_MAX_BYTES = 50 * 1024 * 1024
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
AUDIO_MIME_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/aac",
}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac"}


@dataclass(frozen=True, slots=True)
class MediaAsset:
    id: str
    object_key: str
    asset_type: str
    content_type: str
    size: int
    sha256: str
    status: str
    security_status: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SignedMedia:
    url: str
    expires_at: datetime


class MediaRepository(Protocol):
    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None: ...

    async def save(self, asset: MediaAsset) -> MediaAsset: ...


class InMemoryMediaRepository:
    def __init__(self) -> None:
        self.assets: dict[str, MediaAsset] = {}
        self.by_hash: dict[tuple[str, str], str] = {}

    async def get_by_hash(self, asset_type: str, sha256: str) -> MediaAsset | None:
        asset_id = self.by_hash.get((asset_type, sha256))
        return self.assets.get(asset_id) if asset_id else None

    async def save(self, asset: MediaAsset) -> MediaAsset:
        existing = await self.get_by_hash(asset.asset_type, asset.sha256)
        if existing is not None:
            return existing
        self.assets[asset.id] = asset
        self.by_hash[(asset.asset_type, asset.sha256)] = asset.id
        return asset


class MediaService:
    def __init__(
        self,
        oss: OssProvider,
        repository: MediaRepository,
        *,
        signed_url_ttl_seconds: int = 300,
    ) -> None:
        self._oss = oss
        self._repository = repository
        self._signed_url_ttl_seconds = signed_url_ttl_seconds

    async def create_upload_policy(self, asset_type: str, actor_id: str) -> UploadPolicy:
        max_bytes = self._max_bytes(asset_type)
        prefix = self._prefix(asset_type, actor_id)
        return await self._oss.create_upload_policy(prefix, max_bytes, 600)

    async def confirm_upload(
        self,
        asset_type: str,
        actor_id: str,
        object_key: str,
        now: datetime,
    ) -> MediaAsset:
        prefix = self._prefix(asset_type, actor_id)
        if not object_key.startswith(prefix) or ".." in object_key:
            raise AppError("MEDIA_OBJECT_KEY_INVALID", "素材对象键无效", 422)
        metadata = await self._oss.head_object(object_key)
        self._validate_metadata(asset_type, metadata)
        existing = await self._repository.get_by_hash(asset_type, metadata.sha256)
        if existing is not None:
            return existing
        return await self._repository.save(
            MediaAsset(
                new_ulid(now),
                object_key,
                asset_type,
                metadata.content_type,
                metadata.size,
                metadata.sha256,
                "CONFIRMED",
                "PASSED",
                actor_id,
                now,
            )
        )

    def validate_batch(self, asset_type: str, count: int) -> None:
        limit = 30 if asset_type == "images" else 300 if asset_type == "audio" else 0
        if count < 1 or count > limit:
            raise AppError("MEDIA_BATCH_LIMIT", "素材批次数量超限", 422)

    async def sign_media(
        self,
        object_key: str,
        entitlement_expires_at: datetime | None,
        now: datetime,
    ) -> SignedMedia:
        ttl = self._signed_url_ttl_seconds
        if entitlement_expires_at is not None:
            expires_at = (
                entitlement_expires_at
                if entitlement_expires_at.tzinfo
                else entitlement_expires_at.replace(tzinfo=UTC)
            )
            ttl = min(ttl, int((expires_at - now).total_seconds()))
        if ttl <= 0:
            raise AppError("MEDIA_ACCESS_EXPIRED", "媒体访问权限已到期", 403)
        url = await self._oss.sign_get_url(object_key, ttl)
        return SignedMedia(url, now + timedelta(seconds=ttl))

    @staticmethod
    def _prefix(asset_type: str, actor_id: str) -> str:
        if asset_type not in {"images", "audio"}:
            raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", actor_id):
            raise AppError("MEDIA_ACTOR_INVALID", "上传者标识无效", 422)
        return f"uploads/{asset_type}/{actor_id}/"

    @staticmethod
    def _max_bytes(asset_type: str) -> int:
        if asset_type == "images":
            return IMAGE_MAX_BYTES
        if asset_type == "audio":
            return AUDIO_MAX_BYTES
        raise AppError("MEDIA_TYPE_INVALID", "素材类型无效", 422)

    @staticmethod
    def _validate_metadata(asset_type: str, metadata: ObjectMetadata) -> None:
        allowed_mime = IMAGE_MIME_TYPES if asset_type == "images" else AUDIO_MIME_TYPES
        max_bytes = IMAGE_MAX_BYTES if asset_type == "images" else AUDIO_MAX_BYTES
        if metadata.content_type.lower() not in allowed_mime:
            raise AppError("MEDIA_MIME_INVALID", "素材格式不支持", 422)
        if metadata.size <= 0 or metadata.size > max_bytes:
            raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", metadata.sha256):
            raise AppError("MEDIA_HASH_INVALID", "素材哈希无效", 422)
        if metadata.metadata.get("decodable") != "true":
            raise AppError("MEDIA_DECODE_FAILED", "素材无法解码", 422)
        if metadata.metadata.get("security_status") != "PASSED":
            raise AppError("MEDIA_SECURITY_BLOCKED", "素材未通过安全检查", 422)
        if asset_type == "audio" and not any(
            metadata.object_key.lower().endswith(suffix) for suffix in AUDIO_SUFFIXES
        ):
            raise AppError("MEDIA_EXTENSION_INVALID", "音频扩展名不支持", 422)
