from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.miniapp_api.client import (
    ContactProjection,
    MiniappApiClient,
)
from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class UserProjection:
    user_id: str
    account_status: str
    last_active_at: datetime | None
    formal_entitlement_count: int
    limited_entitlement_count: int
    open_feedback_count: int


@dataclass(frozen=True, slots=True)
class UserDetail:
    projection: UserProjection
    contact: ContactProjection | None
    contact_degraded: bool


class UserProjectionRepository(Protocol):
    async def search(
        self, query: str | None, *, user_ids: tuple[str, ...] | None = None
    ) -> tuple[UserProjection, ...]: ...

    async def get(self, user_id: str) -> UserProjection | None: ...


class InMemoryUserProjectionRepository:
    def __init__(self) -> None:
        self.users: dict[str, UserProjection] = {}

    async def search(
        self, query: str | None, *, user_ids: tuple[str, ...] | None = None
    ) -> tuple[UserProjection, ...]:
        allowed = None if user_ids is None else frozenset(user_ids)
        return tuple(
            user
            for user in self.users.values()
            if (allowed is None or user.user_id in allowed)
            and (query is None or query.lower() in user.user_id.lower())
        )

    async def get(self, user_id: str) -> UserProjection | None:
        return self.users.get(user_id)


class UserProjectionService:
    def __init__(
        self, repository: UserProjectionRepository, miniapp_client: MiniappApiClient
    ) -> None:
        self._repository = repository
        self._miniapp_client = miniapp_client

    async def search(
        self, query: str | None = None, *, wechat_id: str | None = None
    ) -> tuple[UserProjection, ...]:
        if wechat_id is None:
            return await self._repository.search(query)
        user_ids = await self._miniapp_client.search_user_ids_by_wechat(wechat_id)
        return await self._repository.search(None, user_ids=user_ids)

    async def detail(self, user_id: str) -> UserDetail:
        projection = await self._repository.get(user_id)
        if projection is None:
            raise AppError("USER_NOT_FOUND", "用户不存在", 404)
        contacts = await self._miniapp_client.get_contact_projections((user_id,))
        contact = next((item for item in contacts.contacts if item.user_id == user_id), None)
        return UserDetail(projection, contact, contacts.degraded)
