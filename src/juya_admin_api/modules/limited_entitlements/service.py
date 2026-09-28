from datetime import datetime

from juya_admin_api.modules.access_policy.domain import AccessDecision, AccessLevel
from juya_admin_api.modules.limited_entitlements.domain import (
    LimitedEntitlement,
    RemedyMode,
    calculate_pause,
    calculate_remedy,
    calculate_resume,
    calculate_revoke,
)
from juya_admin_api.modules.limited_entitlements.repository import (
    LimitedEntitlementRepository,
)


class LimitedEntitlementService:
    def __init__(self, repository: LimitedEntitlementRepository) -> None:
        self._repository = repository

    async def grant(
        self,
        user_id: str,
        campaign_version_id: str,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement:
        return await self._repository.grant(
            user_id, campaign_version_id, actor_id, idempotency_key, now
        )

    async def activate_for_scene(
        self, user_id: str, scene_id: str, now: datetime
    ) -> AccessDecision:
        entitlement = await self._repository.activate_for_scene(user_id, scene_id, now)
        if (
            entitlement is None
            or entitlement.status != "ACTIVE"
            or entitlement.expires_at is None
            or now >= entitlement.expires_at
        ):
            return AccessDecision(AccessLevel.HIDDEN, (), None)
        return AccessDecision(
            AccessLevel.LIMITED,
            (f"LIMITED:{entitlement.id}",),
            entitlement.expires_at,
            entitlement.activated_at,
        )

    async def remedy(
        self,
        entitlement_id: str,
        mode: RemedyMode,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> LimitedEntitlement:
        return await self._repository.change(
            entitlement_id,
            actor_id,
            idempotency_key,
            "REMEDY",
            now,
            lambda current: calculate_remedy(current, mode, now),
        )

    async def pause(
        self, entitlement_id: str, actor_id: str, reason: str, now: datetime
    ) -> LimitedEntitlement:
        return await self._repository.change(
            entitlement_id,
            actor_id,
            f"pause:{entitlement_id}:{now.isoformat()}",
            "PAUSE",
            now,
            lambda current: calculate_pause(current, now),
            reason=reason,
        )

    async def resume(self, entitlement_id: str, actor_id: str, now: datetime) -> LimitedEntitlement:
        return await self._repository.change(
            entitlement_id,
            actor_id,
            f"resume:{entitlement_id}:{now.isoformat()}",
            "RESUME",
            now,
            lambda current: calculate_resume(current, now),
        )

    async def revoke(
        self, entitlement_id: str, actor_id: str, reason: str, now: datetime
    ) -> LimitedEntitlement:
        return await self._repository.change(
            entitlement_id,
            actor_id,
            f"revoke:{entitlement_id}:{now.isoformat()}",
            "REVOKE",
            now,
            calculate_revoke,
            reason=reason,
        )
