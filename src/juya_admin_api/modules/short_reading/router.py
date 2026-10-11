"""Admin short-reading commands; all writes require authenticated CSRF dependency."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.modules.short_reading.store import ShortReadingStore

AdminDependency = Callable[..., Awaitable[SessionRecord]]


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series_id: str
    content: ReadingContent = Field(default_factory=ReadingContent)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class RevisionRequest(CommandRequest):
    source_revision_id: str | None = None


class SaveRequest(CommandRequest):
    expected_edit_version: int = Field(ge=1)
    content: ReadingContent


class PublishRequest(CommandRequest):
    manual_confirmed: bool = False


class TrialRequest(CommandRequest):
    enabled: bool


class SortOrderRequest(CommandRequest):
    sort_order: int = Field(strict=True, ge=-2147483648, le=2147483647)


def create_short_reading_router(
    store: ShortReadingStore,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content/short-readings", tags=["short-readings"])

    @router.get("")
    async def list_readings(
        admin: Annotated[SessionRecord, Depends(current_admin)],
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        status: Literal["DRAFT", "PUBLISHED", "OFFLINE"] | None = Query(None),
        series_id: str | None = Query(None),
        query: str = Query("", max_length=200),
    ) -> dict[str, Any]:
        filters = []
        parameters: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
        if status:
            filters.append("s.status=:status")
            parameters["status"] = status
        if series_id:
            filters.append("s.series_id=:series")
            parameters["series"] = series_id
        if query.strip():
            filters.append(
                "(CAST(JSON_EXTRACT(r.content_snapshot,'$.title_en') AS CHAR) LIKE :query "
                "OR CAST(JSON_EXTRACT(r.content_snapshot,'$.title_zh') AS CHAR) LIKE :query)"
            )
            parameters["query"] = "%" + query.strip() + "%"
        source = (
            " FROM short_reading s LEFT JOIN short_reading_trial t "
            "ON t.short_reading_id=s.public_id LEFT JOIN short_reading_revision r "
            "ON r.public_id=CASE WHEN s.status='PUBLISHED' THEN s.published_revision_id "
            "ELSE COALESCE(s.draft_revision_id,s.published_revision_id) END"
        )
        where = " WHERE " + " AND ".join(filters) if filters else ""
        async with store.sessions() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT s.*,COALESCE(t.enabled,0) AS trial_enabled"
                            + source
                            + where
                            + " ORDER BY s.sort_order ASC,s.public_id ASC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        parameters,
                    )
                )
                .mappings()
                .all()
            )
            total = await session.scalar(text("SELECT COUNT(*)" + source + where), parameters)
            return {
                "items": [
                    {**dict(row), "trial_enabled": bool(row["trial_enabled"])} for row in rows
                ],
                "page": page,
                "page_size": page_size,
                "total": total,
            }

    @router.post("")
    async def create(
        payload: CreateRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "create", "new", str(admin.admin_user_id), key, payload.model_dump(mode="json")
        )

    @router.post("/{identity}/sort-order")
    async def sort_order(
        identity: str,
        payload: SortOrderRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "sort-order", identity, str(admin.admin_user_id), key, payload.model_dump(mode="json")
        )

    @router.get("/{identity}")
    async def detail(
        identity: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        async with store.sessions() as session:
            return await store.row(session, identity)

    @router.get("/{identity}/revisions")
    async def revisions(
        identity: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        async with store.sessions() as session:
            await store.row(session, identity)
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id,version_no,edit_version,status,"
                            "source_revision_id,content_hash,created_by,created_at "
                            "FROM short_reading_revision WHERE short_reading_id=:id "
                            "ORDER BY version_no DESC"
                        ),
                        {"id": identity},
                    )
                )
                .mappings()
                .all()
            )
            return {"items": [dict(row) for row in rows]}

    @router.get("/{identity}/revisions/{revision_id}")
    async def revision(
        identity: str, revision_id: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        async with store.sessions() as session:
            return await store.revision(session, identity, revision_id)

    @router.post("/{identity}/revisions")
    async def create_revision(
        identity: str,
        payload: RevisionRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "revision", identity, str(admin.admin_user_id), key, payload.model_dump(mode="json")
        )

    @router.put("/{identity}/revisions/{revision_id}")
    async def save(
        identity: str,
        revision_id: str,
        payload: SaveRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "save",
            identity,
            str(admin.admin_user_id),
            key,
            {**payload.model_dump(mode="json"), "revision_id": revision_id},
        )

    @router.post("/{identity}/revisions/{revision_id}/publish-check")
    async def check(
        identity: str, revision_id: str, admin: Annotated[SessionRecord, Depends(current_admin)]
    ) -> dict[str, Any]:
        async with store.sessions() as session, session.begin():
            revision = await store.revision(session, identity, revision_id)
            errors = await store.inspect(session, revision)
            return {
                "revision_id": revision_id,
                "content_hash": revision["content_hash"],
                "ready": not errors,
                "error_codes": errors,
            }

    @router.post("/{identity}/revisions/{revision_id}/publish")
    async def publish(
        identity: str,
        revision_id: str,
        payload: PublishRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "publish",
            identity,
            str(admin.admin_user_id),
            key,
            {**payload.model_dump(mode="json"), "revision_id": revision_id},
        )

    @router.post("/{identity}/offline")
    async def offline(
        identity: str,
        payload: CommandRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "offline", identity, str(admin.admin_user_id), key, payload.model_dump(mode="json")
        )

    @router.put("/{identity}/trial")
    async def trial(
        identity: str,
        payload: TrialRequest,
        admin: Annotated[SessionRecord, Depends(current_admin_write)],
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, Any]:
        return await store.command(
            "trial", identity, str(admin.admin_user_id), key, payload.model_dump(mode="json")
        )

    return router
