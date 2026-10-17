"""Runtime ports. All callbacks use the supplied shared transaction, never a new session."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.content.v15_contracts import (
    ContentType,
    SceneDetail,
    ShortReadingDetail,
)

ContentDetail = SceneDetail | ShortReadingDetail


@dataclass(frozen=True)
class AccessFacts:
    full_access: bool
    visible: bool
    module_version: int
    authorization_version: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ResourceRequest:
    user_id: str
    content_type: ContentType
    content_id: str
    revision_id: str
    target_id: str
    resource_version: str | None
    role: Literal["COVER", "LEARNING_IMAGE", "WORD_IMAGE", "WHOLE", "SENTENCE", "WORD", "CHUNK"]
    favorite_id: str | None = None
    asset_id: str | None = None
    preview_only: bool = False


class ContentLoader(Protocol):
    async def __call__(
        self, session: AsyncSession, content_type: ContentType, content_id: str, /
    ) -> ContentDetail: ...


class Authorizer(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        user_id: str,
        content_type: ContentType,
        content_id: str,
        now: datetime,
        /,
        *,
        lock: bool,
    ) -> AccessFacts:
        """Check live facts; lock=True holds shared SQL locks until commit."""
        ...


class ResourceSigner(Protocol):
    async def __call__(
        self, session: AsyncSession, request: ResourceRequest, expires_at: datetime, /
    ) -> dict[str, Any]:
        """Recheck fixed media, security, role and expiry; never sign arbitrary IDs."""
        ...


@dataclass(frozen=True)
class FavoriteFacts:
    favorite_id: str
    snapshot_revision_id: str
    content_type: ContentType
    content_id: str
    revision_id: str | None
    saved_content_version: int | None
    entry_id: str
    sense_id: str
    source_id: str
    entry_type: Literal["word", "chunk"]
    image_asset_id: str | None
    pronunciation_target_id: str | None
    resource_version: str | None
    safe_title_en: str = ""
    safe_title_zh: str = ""


class FavoriteAuthority(Protocol):
    async def __call__(
        self, session: AsyncSession, user_id: str, favorite_id: str, /
    ) -> FavoriteFacts:
        """Read active owned BFF snapshot and account; never use retention projection."""
        ...
