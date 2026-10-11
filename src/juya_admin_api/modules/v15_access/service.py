"""Same-transaction authorization, immutable confirmation and purpose-limited resources."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.content.v15_contracts import (
    ConfirmationCredential,
    ConfirmedEntry,
    ContentType,
    FavoriteReference,
    FavoriteResourceEvent,
    SceneDetail,
    SourceResolution,
)
from juya_admin_api.modules.v15_access.credentials import CredentialCodec, details_hash
from juya_admin_api.modules.v15_access.ports import (
    AccessFacts,
    Authorizer,
    ContentDetail,
    ContentLoader,
    FavoriteAuthority,
    FavoriteFacts,
    ResourceRequest,
    ResourceSigner,
)
from juya_admin_api.shared.errors import AppError


class V15AccessService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        content_loader: ContentLoader,
        authorizer: Authorizer,
        resource_signer: ResourceSigner,
        credential_secret: bytes,
        favorite_authority: FavoriteAuthority,
        ttl_seconds: int = 60,
    ) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("V1.5 resource and confirmation TTL must be between 1 and 60 seconds")
        self.sessions = sessions
        self.content_loader = content_loader
        self.authorizer = authorizer
        self.resource_signer = resource_signer
        self.favorite_authority = favorite_authority
        self.codec = CredentialCodec(credential_secret)
        self.ttl_seconds = ttl_seconds

    def expiry(self, now: datetime, facts: AccessFacts | None = None) -> datetime:
        expiry = now + timedelta(seconds=self.ttl_seconds)
        if facts and facts.expires_at:
            expiry = min(expiry, facts.expires_at)
        if expiry <= now:
            raise AppError("CONTENT_ACCESS_DENIED", "当前账号暂未开通此内容", 403)
        return expiry

    async def authorized_within(
        self,
        session: AsyncSession,
        user_id: str,
        content_type: ContentType,
        content_id: str,
        now: datetime,
        *,
        lock: bool = False,
        preview: bool = False,
    ) -> tuple[ContentDetail, AccessFacts]:
        facts = await self.authorizer(session, user_id, content_type, content_id, now, lock=lock)
        if not facts.visible if preview else not facts.full_access:
            raise AppError("CONTENT_ACCESS_DENIED", "当前账号暂未开通此内容", 403)
        self.expiry(now, facts)
        detail = await self.content_loader(session, content_type, content_id)
        if detail.content_id != content_id or detail.content_type != content_type:
            raise AppError("CONTENT_IDENTITY_CONFLICT", "内容类型或标识不一致", 409)
        return detail, facts

    async def open(
        self, user_id: str, content_type: ContentType, content_id: str, now: datetime
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            detail, _ = await self.authorized_within(
                session, user_id, content_type, content_id, now, lock=True
            )
            return detail.model_dump(mode="json")

    async def preview(
        self, user_id: str, content_type: ContentType, content_id: str, now: datetime
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            detail, _ = await self.authorized_within(
                session, user_id, content_type, content_id, now, preview=True
            )
            return detail.model_dump(
                mode="json",
                include={
                    "content_id",
                    "content_type",
                    "content_version",
                    "title_en",
                    "title_zh",
                    "category",
                    "topic",
                    "level",
                    "safe_intro",
                    "cover_asset_id",
                },
            )

    @staticmethod
    def entry(detail: ContentDetail, reference: FavoriteReference) -> ConfirmedEntry:
        if not isinstance(detail, SceneDetail):
            raise AppError("SHORT_READING_HAS_NO_ENTRIES", "纯阅读短文不提供词卡收藏", 422)
        if reference.content_version != detail.content_version:
            raise AppError("CONTENT_VERSION_CONFLICT", "内容版本已变化,请刷新", 409)
        entries = [
            entry
            for entry in [*detail.words, *detail.chunks]
            if (entry.entry_id, entry.sense_id, entry.source_id)
            == (reference.entry_id, reference.sense_id, reference.source_id)
        ]
        if len(entries) != 1:
            raise AppError("ENTRY_IDENTITY_CONFLICT", "本篇义项或来源不匹配", 409)
        return entries[0]

    async def confirmation(
        self, user_id: str, reference: FavoriteReference, now: datetime
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            detail, facts = await self.authorized_within(
                session, user_id, reference.content_type, reference.content_id, now, lock=True
            )
            entry = self.entry(detail, reference)
            claims = {
                **reference.model_dump(),
                "user_id": user_id,
                "revision_id": detail.revision_id,
                "details_hash": details_hash(entry.model_dump(mode="json")),
                "module_version": facts.module_version,
                "authorization_version": facts.authorization_version,
                "expires_at": self.expiry(now, facts).isoformat(),
            }
            credential = ConfirmationCredential(**claims, token=self.codec.sign(claims))
            return {
                "credential": credential.model_dump(mode="json"),
                "entry": entry.model_dump(mode="json"),
                "safe_summary": {"title_en": detail.title_en, "title_zh": detail.title_zh},
            }

    async def verify_confirmation_within(
        self, session: AsyncSession, credential: ConfirmationCredential, user_id: str, now: datetime
    ) -> ConfirmedEntry:
        """BFF calls inside its snapshot-write transaction; HTTP alone cannot transfer locks."""
        claims = self.codec.verify(credential.token, now)
        if credential.user_id != user_id or claims != credential.model_dump(exclude={"token"}):
            raise AppError("CONFIRMATION_CREDENTIAL_INVALID", "收藏确认身份不一致", 409)
        detail, facts = await self.authorized_within(
            session, user_id, credential.content_type, credential.content_id, now, lock=True
        )
        entry = self.entry(detail, credential)
        if (
            credential.revision_id != detail.revision_id
            or credential.details_hash != details_hash(entry.model_dump(mode="json"))
            or credential.module_version != facts.module_version
            or credential.authorization_version != facts.authorization_version
        ):
            raise AppError("CONTENT_VERSION_CONFLICT", "内容或授权已变化,请刷新", 409)
        return entry

    async def source_resolution(
        self, user_id: str, favorite_id: str, now: datetime
    ) -> SourceResolution:
        async with self.sessions() as session, session.begin():
            saved = await self.favorite_authority(session, user_id, favorite_id)
            summary = {"title_en": saved.safe_title_en, "title_zh": saved.safe_title_zh}
            facts = await self.authorizer(
                session, user_id, saved.content_type, saved.content_id, now, lock=True
            )
            if not facts.full_access:
                return SourceResolution(
                    saved_content_version=saved.saved_content_version,
                    target_content_version=None,
                    status="NO_ACCESS",
                    locator=None,
                    safe_summary=summary,
                )
            detail = await self.content_loader(session, saved.content_type, saved.content_id)
            locator = None
            status: Literal["EXACT", "UPDATED_OR_UNRESOLVED", "NO_ACCESS"] = "UPDATED_OR_UNRESOLVED"
            if isinstance(detail, SceneDetail):
                matches = [
                    entry
                    for entry in [*detail.words, *detail.chunks]
                    if (entry.entry_id, entry.sense_id, entry.source_id)
                    == (saved.entry_id, saved.sense_id, saved.source_id)
                ]
                if len(matches) == 1:
                    candidate = matches[0].source_locator
                    if (
                        candidate
                        and candidate.sentence_id
                        and sum(
                            sentence.sentence_id == candidate.sentence_id
                            for sentence in detail.dialogue
                        )
                        == 1
                    ):
                        locator, status = candidate, "EXACT"
                    elif detail.content_type == "word_card" and candidate is None:
                        status = "EXACT"
            return SourceResolution(
                saved_content_version=saved.saved_content_version,
                target_content_version=detail.content_version,
                status=status,
                locator=locator,
                safe_summary=summary,
            )

    @staticmethod
    def current_resource(
        detail: ContentDetail, user_id: str, target_id: str, resource_version: str | None
    ) -> ResourceRequest:
        candidates: list[ResourceRequest] = []
        base: dict[str, Any] = dict(
            user_id=user_id,
            content_type=detail.content_type,
            content_id=detail.content_id,
            revision_id=detail.revision_id,
        )
        if detail.whole_audio:
            audio = detail.whole_audio
            candidates.append(
                ResourceRequest(
                    **base,
                    target_id=audio.target_id,
                    resource_version=audio.resource_version,
                    role="WHOLE",
                )
            )
        if detail.cover_asset_id:
            candidates.append(
                ResourceRequest(
                    **base,
                    target_id=detail.cover_asset_id,
                    resource_version=None,
                    role="COVER",
                    asset_id=detail.cover_asset_id,
                )
            )
        if isinstance(detail, SceneDetail):
            if detail.learning_image_asset_id:
                candidates.append(
                    ResourceRequest(
                        **base,
                        target_id=detail.learning_image_asset_id,
                        resource_version=None,
                        role="LEARNING_IMAGE",
                        asset_id=detail.learning_image_asset_id,
                    )
                )
            for sentence in detail.dialogue:
                if sentence.slice_target_id and sentence.slice_version:
                    candidates.append(
                        ResourceRequest(
                            **base,
                            target_id=sentence.slice_target_id,
                            resource_version=sentence.slice_version,
                            role="SENTENCE",
                        )
                    )
            for entry in [*detail.words, *detail.chunks]:
                if entry.image_asset_id and entry.entry_type == "word":
                    candidates.append(
                        ResourceRequest(
                            **base,
                            target_id=entry.image_asset_id,
                            resource_version=None,
                            role="WORD_IMAGE",
                            asset_id=entry.image_asset_id,
                        )
                    )
                if entry.pronunciation and entry.pronunciation.resource_version:
                    candidates.append(
                        ResourceRequest(
                            **base,
                            target_id=entry.pronunciation.target_id,
                            resource_version=entry.pronunciation.resource_version,
                            role="WORD" if entry.entry_type == "word" else "CHUNK",
                        )
                    )
        matches = [
            candidate
            for candidate in candidates
            if candidate.target_id == target_id and candidate.resource_version == resource_version
        ]
        if not matches or any(candidate != matches[0] for candidate in matches):
            raise AppError("RESOURCE_REFERENCE_INVALID", "资源不属于当前授权版本", 403)
        return matches[0]

    async def content_resource(
        self,
        user_id: str,
        content_type: ContentType,
        content_id: str,
        target_id: str,
        resource_version: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            detail, facts = await self.authorized_within(
                session, user_id, content_type, content_id, now, lock=True
            )
            request = self.current_resource(detail, user_id, target_id, resource_version)
            return await self.resource_signer(session, request, self.expiry(now, facts))

    @staticmethod
    def favorite_resource(
        saved: FavoriteFacts, user_id: str, target_id: str, resource_version: str | None
    ) -> ResourceRequest:
        role: Literal["WORD_IMAGE", "WORD", "CHUNK"] | None = None
        if (
            saved.entry_type == "word"
            and saved.image_asset_id == target_id
            and resource_version is None
        ):
            role = "WORD_IMAGE"
        elif (
            saved.pronunciation_target_id == target_id
            and resource_version is not None
            and saved.resource_version == resource_version
        ):
            role = "WORD" if saved.entry_type == "word" else "CHUNK"
        if role is None:
            raise AppError("FAVORITE_RESOURCE_PURPOSE_INVALID", "收藏仅允许固定词图或词语发音", 403)
        return ResourceRequest(
            user_id=user_id,
            content_type=saved.content_type,
            content_id=saved.content_id,
            revision_id=saved.revision_id,
            target_id=target_id,
            resource_version=resource_version,
            role=role,
            favorite_id=saved.favorite_id,
            asset_id=saved.image_asset_id if role == "WORD_IMAGE" else None,
        )

    async def review_resource(
        self,
        user_id: str,
        favorite_id: str,
        target_id: str,
        resource_version: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            saved = await self.favorite_authority(session, user_id, favorite_id)
            request = self.favorite_resource(saved, user_id, target_id, resource_version)
            # No source/module/grant gate here: owned active fixed snapshot remains reviewable.
            return await self.resource_signer(session, request, self.expiry(now))

    async def project_event(self, event: FavoriteResourceEvent) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            authority = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM favorite_resource_reference "
                            "WHERE favorite_id=:id FOR SHARE"
                        ),
                        {"id": event.favorite_id},
                    )
                )
                .mappings()
                .first()
            )
            if authority is None:
                raise AppError("FAVORITE_REFERENCE_NOT_FOUND", "收藏权威资源事件不存在", 409)
            if int(authority["sequence"]) > event.sequence:
                return {"applied": False, "reason": "STALE", "sequence": int(authority["sequence"])}
            self.validate_projection_event(dict(authority), event)
            current = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM favorite_resource_projection "
                            "WHERE favorite_id=:id FOR UPDATE"
                        ),
                        {"id": event.favorite_id},
                    )
                )
                .mappings()
                .first()
            )
            if current and int(current["sequence"]) >= event.sequence:
                if int(current["sequence"]) == event.sequence:
                    self.validate_projection_event(dict(current), event)
                return {
                    "applied": False,
                    "reason": "REPLAY_OR_STALE",
                    "sequence": current["sequence"],
                }
            params = {**event.model_dump(), "now": datetime.now(UTC)}
            await session.execute(
                text(
                    "INSERT INTO favorite_resource_projection(favorite_id,"
                    "snapshot_revision_id,content_type,content_id,revision_id,image_asset_id,"
                    "pronunciation_target_id,resource_version,active,event_id,sequence,updated_at) "
                    "VALUES(:favorite_id,:snapshot_revision_id,:content_type,:content_id,:revision_id,"
                    ":image_asset_id,:pronunciation_target_id,:resource_version,:active,:event_id,"
                    ":sequence,:now) ON DUPLICATE KEY UPDATE "
                    "snapshot_revision_id=:snapshot_revision_id,"
                    "content_type=:content_type,content_id=:content_id,revision_id=:revision_id,"
                    "image_asset_id=:image_asset_id,pronunciation_target_id=:pronunciation_target_id,"
                    "resource_version=:resource_version,active=:active,event_id=:event_id,"
                    "sequence=:sequence,updated_at=:now"
                ),
                params,
            )
            return {"applied": True, "sequence": event.sequence}

    @staticmethod
    def validate_projection_event(authority: dict[str, Any], event: FavoriteResourceEvent) -> None:
        if any(authority.get(key) != value for key, value in event.model_dump().items()):
            raise AppError("FAVORITE_REFERENCE_EVENT_CONFLICT", "收藏资源事件与权威记录不一致", 409)
