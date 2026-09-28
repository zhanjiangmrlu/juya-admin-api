from datetime import datetime
from typing import Protocol

from juya_admin_api.modules.access_policy.domain import (
    AccessDecision,
    AccessGrant,
    AccessLevel,
)


class ContentAccessPort(Protocol):
    async def is_open(self, scene_id: str, now: datetime) -> bool: ...

    async def is_preview(self, scene_id: str, now: datetime) -> bool: ...


class FormalGrantPort(Protocol):
    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]: ...


class LimitedGrantPort(Protocol):
    async def active_grants(
        self, user_id: str, scene_id: str, now: datetime
    ) -> tuple[AccessGrant, ...]: ...


class AccessPolicyService:
    def __init__(
        self,
        content: ContentAccessPort,
        formal_grants: FormalGrantPort,
        limited_grants: LimitedGrantPort,
    ) -> None:
        self._content = content
        self._formal_grants = formal_grants
        self._limited_grants = limited_grants

    async def authorize(self, user_id: str, scene_id: str, now: datetime) -> AccessDecision:
        opened = await self._content.is_open(scene_id, now)
        formal = self._active(await self._formal_grants.active_grants(user_id, scene_id, now), now)
        limited = self._active(
            await self._limited_grants.active_grants(user_id, scene_id, now), now
        )

        if opened or formal or limited:
            sources = (
                (("OPEN",) if opened else ())
                + tuple(grant.source for grant in formal)
                + tuple(grant.source for grant in limited)
            )
            expiries = tuple(
                grant.expires_at for grant in (*formal, *limited) if grant.expires_at is not None
            )
            level = (
                AccessLevel.OPEN
                if opened
                else AccessLevel.FORMAL
                if formal
                else AccessLevel.LIMITED
            )
            return AccessDecision(
                level,
                sources,
                min(expiries) if expiries else None,
            )

        if await self._content.is_preview(scene_id, now):
            return AccessDecision(AccessLevel.PREVIEW, ("PREVIEW",), None)
        return AccessDecision(AccessLevel.HIDDEN, (), None)

    @staticmethod
    def _active(grants: tuple[AccessGrant, ...], now: datetime) -> tuple[AccessGrant, ...]:
        return tuple(
            grant for grant in grants if grant.expires_at is None or now < grant.expires_at
        )
