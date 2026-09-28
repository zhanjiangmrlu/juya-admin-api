from datetime import UTC
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.user_projection.service import UserProjection


class SQLAlchemyUserProjectionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search(
        self, query: str | None, *, user_ids: tuple[str, ...] | None = None
    ) -> tuple[UserProjection, ...]:
        clauses = ["1 = 1"]
        parameters: dict[str, object] = {}
        if query:
            clauses.append("u.public_id LIKE :query")
            parameters["query"] = f"%{query}%"
        if user_ids is not None:
            if not user_ids:
                return ()
            placeholders = []
            for index, user_id in enumerate(user_ids):
                name = f"user_id_{index}"
                placeholders.append(f":{name}")
                parameters[name] = user_id
            clauses.append(f"u.public_id IN ({','.join(placeholders)})")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT u.public_id, p.account_status, p.last_active_at, "
                        "p.formal_entitlement_count, p.limited_entitlement_count, "
                        "p.open_feedback_count FROM user_admin_projection p "
                        "JOIN user_account u ON u.id = p.user_id WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY p.last_active_at DESC LIMIT 100"
                    ),
                    parameters,
                )
            ).all()
        return tuple(_from_row(row) for row in rows)

    async def get(self, user_id: str) -> UserProjection | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT u.public_id, p.account_status, p.last_active_at, "
                        "p.formal_entitlement_count, p.limited_entitlement_count, "
                        "p.open_feedback_count FROM user_admin_projection p "
                        "JOIN user_account u ON u.id = p.user_id "
                        "WHERE u.public_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            ).first()
        return None if row is None else _from_row(row)


def _from_row(row: Any) -> UserProjection:
    last_active_at = row.last_active_at
    if last_active_at is not None and last_active_at.tzinfo is None:
        last_active_at = last_active_at.replace(tzinfo=UTC)
    return UserProjection(
        user_id=row.public_id,
        account_status=row.account_status,
        last_active_at=last_active_at,
        formal_entitlement_count=row.formal_entitlement_count,
        limited_entitlement_count=row.limited_entitlement_count,
        open_feedback_count=row.open_feedback_count,
    )
