from datetime import datetime

from juya_admin_api.modules.formal_entitlements.domain import (
    FormalEntitlement,
    FormalEntitlementCommand,
    calculate_operation,
)
from juya_admin_api.modules.formal_entitlements.repository import (
    FormalEntitlementRepository,
)


class FormalEntitlementService:
    def __init__(self, repository: FormalEntitlementRepository) -> None:
        self._repository = repository

    async def preview_operation(
        self, command: FormalEntitlementCommand, now: datetime
    ) -> FormalEntitlement:
        current = await self._repository.get(command.user_id, command.package_id)
        return calculate_operation(current, command, now)

    async def apply_operation(
        self,
        command: FormalEntitlementCommand,
        actor: str,
        idempotency_key: str,
        now: datetime,
    ) -> FormalEntitlement:
        return await self._repository.apply(
            command,
            actor,
            idempotency_key,
            now,
            calculate_operation,
        )
