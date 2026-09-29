from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from juya_admin_api.integrations.miniapp_api.client import (
    ContactProjection,
    LearningOverview,
    MiniappApiClient,
)
from juya_admin_api.modules.audit.service import AuditEvent, AuditService
from juya_admin_api.shared.errors import AppError

_CONTACT_STATUSES = frozenset(
    {"NOT_PROVIDED", "PENDING", "CONTACTED", "UNREACHABLE", "DO_NOT_CONTACT"}
)


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
    learning_overview: LearningOverview | None
    learning_degraded: bool


@dataclass(frozen=True, slots=True)
class UserListItem:
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
        self,
        repository: UserProjectionRepository,
        miniapp_client: MiniappApiClient,
        audit: AuditService,
    ) -> None:
        self._repository = repository
        self._miniapp_client = miniapp_client
        self._audit = audit

    async def search(
        self,
        query: str | None = None,
        *,
        wechat_id: str | None = None,
        contact_status: str | None = None,
        admin_id: str,
        request_id: str,
        occurred_at: datetime,
    ) -> tuple[UserListItem, ...]:
        if contact_status is not None and contact_status not in _CONTACT_STATUSES:
            raise AppError("CONTACT_STATUS_INVALID", "联系状态无效", 422)
        user_ids = None
        if wechat_id is not None:
            user_ids = await self._miniapp_client.search_user_ids_by_wechat(wechat_id, admin_id)
        projections = await self._repository.search(query, user_ids=user_ids)
        if not projections:
            return ()
        contacts = await self._miniapp_client.get_contact_projections(
            tuple(item.user_id for item in projections), admin_id
        )
        if contacts.degraded and contact_status is not None:
            raise AppError("MINIAPP_API_UNAVAILABLE", "联系资料服务暂不可用", 503)
        by_user_id = {item.user_id: item for item in contacts.contacts}
        items = tuple(
            UserListItem(projection, by_user_id.get(projection.user_id), contacts.degraded)
            for projection in projections
            if contact_status is None
            or (
                (contact := by_user_id.get(projection.user_id)) is not None
                and contact.contact_status == contact_status
            )
        )
        sensitive_ids = [
            item.projection.user_id
            for item in items
            if item.contact is not None and item.contact.wechat_id is not None
        ]
        if sensitive_ids:
            await self._audit.record(
                AuditEvent(
                    actor_public_id=admin_id,
                    action="contact.view.list",
                    object_type="user_contact",
                    object_public_id="users",
                    before_summary={},
                    after_summary={"hit_count": len(sensitive_ids), "user_ids": sensitive_ids},
                    reason=None,
                    request_id=request_id,
                    occurred_at=occurred_at,
                )
            )
        return items

    async def detail(
        self,
        user_id: str,
        *,
        admin_id: str,
        request_id: str,
        occurred_at: datetime,
    ) -> UserDetail:
        projection = await self._repository.get(user_id)
        if projection is None:
            raise AppError("USER_NOT_FOUND", "用户不存在", 404)
        contacts = await self._miniapp_client.get_contact_projections((user_id,), admin_id)
        contact = next((item for item in contacts.contacts if item.user_id == user_id), None)
        learning_degraded = False
        try:
            learning = await self._miniapp_client.get_learning_overview(user_id, admin_id)
        except AppError as error:
            if error.code != "MINIAPP_API_UNAVAILABLE":
                raise
            learning = None
            learning_degraded = True
        if contact is not None and contact.wechat_id is not None:
            await self._audit.record(
                AuditEvent(
                    actor_public_id=admin_id,
                    action="contact.view.detail",
                    object_type="user_contact",
                    object_public_id=user_id,
                    before_summary={},
                    after_summary={"hit_count": 1, "user_id": user_id},
                    reason=None,
                    request_id=request_id,
                    occurred_at=occurred_at,
                )
            )
        return UserDetail(
            projection,
            contact,
            contacts.degraded,
            learning,
            learning_degraded,
        )
