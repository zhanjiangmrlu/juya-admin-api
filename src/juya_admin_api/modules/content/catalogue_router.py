"""Authenticated, server-paged catalogue for all three content templates."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.modules.admin_auth.domain import SessionRecord

ContentType = Literal["dialogue", "word_card", "short_reading"]
ContentStatus = Literal["DRAFT", "PUBLISHED", "OFFLINE"]


class CatalogueItem(BaseModel):
    id: str
    content_type: ContentType
    title_en: str
    title_zh: str
    series_id: str
    series_title: str
    status: ContentStatus
    draft_revision_id: str | None
    published_revision_id: str | None
    updated_at: datetime | None


class CataloguePage(BaseModel):
    items: list[CatalogueItem]
    total: int
    page: int
    page_size: int


CATALOGUE = """
 SELECT s.public_id AS id,
   CASE WHEN ct.template_type IN ('vocabulary','word_card')
        THEN 'word_card' ELSE 'dialogue' END AS content_type,
   COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_en')),''),
            s.title,'') AS title_en,
   COALESCE(JSON_UNQUOTE(JSON_EXTRACT(r.content_snapshot,'$.title_zh')),'') AS title_zh,
   cs.public_id AS series_id,cs.title AS series_title,s.status,
   draft.public_id AS draft_revision_id,published.public_id AS published_revision_id,
   s.updated_at
 FROM scene s JOIN content_series cs ON cs.id=s.series_id
 LEFT JOIN content_template ct ON ct.id=s.template_id
 LEFT JOIN scene_revision draft ON draft.id=s.draft_revision_id
 LEFT JOIN scene_revision published ON published.id=s.published_revision_id
 LEFT JOIN scene_revision r ON r.id=COALESCE(s.draft_revision_id,s.published_revision_id)
 UNION ALL
 SELECT sr.public_id AS id,'short_reading' AS content_type,
   COALESCE(JSON_UNQUOTE(JSON_EXTRACT(rr.content_snapshot,'$.title_en')),'') AS title_en,
   COALESCE(JSON_UNQUOTE(JSON_EXTRACT(rr.content_snapshot,'$.title_zh')),'') AS title_zh,
   sr.series_id,COALESCE(cs.title,'') AS series_title,sr.status,
   sr.draft_revision_id,sr.published_revision_id,sr.updated_at
 FROM short_reading sr LEFT JOIN content_series cs ON cs.public_id=sr.series_id
 LEFT JOIN short_reading_revision rr
   ON rr.public_id=COALESCE(sr.draft_revision_id,sr.published_revision_id)
"""
WHERE = """
 WHERE (:query IS NULL OR id LIKE :query ESCAPE '!'
   OR title_en LIKE :query ESCAPE '!' OR title_zh LIKE :query ESCAPE '!')
 AND (:content_type IS NULL OR content_type=:content_type)
 AND (:series_id IS NULL OR series_id=:series_id)
 AND (:status IS NULL OR status=:status)
"""


async def read_catalogue(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    query: str | None = None,
    content_type: ContentType | None = None,
    series_id: str | None = None,
    status: ContentStatus | None = None,
) -> dict[str, Any]:
    """Filter both namespaces before counting and paging; titles come from actual revisions.

    session is the authenticated request's read transaction; page and page_size define the
    global page, query matches literal bilingual titles or IDs, content_type selects a template,
    series_id selects its stable category, and status selects the online content state.
    """
    keyword = (query or "").strip()
    escaped = keyword.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    params = {
        "query": f"%{escaped}%" if keyword else None,
        "content_type": content_type,
        "series_id": series_id or None,
        "status": status,
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    source = f"FROM ({CATALOGUE}) AS catalogue {WHERE}"
    total = int(await session.scalar(text("SELECT COUNT(*) " + source), params) or 0)
    rows = await session.execute(
        text("SELECT * " + source + " ORDER BY id,content_type LIMIT :limit OFFSET :offset"),
        params,
    )
    return {
        "items": [dict(row) for row in rows.mappings().all()],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def create_catalogue_router(
    sessions: async_sessionmaker[AsyncSession],
    *,
    current_admin: Callable[..., Awaitable[SessionRecord]],
) -> APIRouter:
    """Create the catalogue reader using the shared session factory and admin dependency."""
    router = APIRouter(prefix="/api/v1/admin/content", tags=["admin-content"])

    @router.get("/catalogue", response_model=CataloguePage)
    async def catalogue(
        _admin: Annotated[SessionRecord, Depends(current_admin)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        query: Annotated[str | None, Query(max_length=200)] = None,
        content_type: ContentType | None = None,
        series_id: str | None = None,
        status: ContentStatus | None = None,
    ) -> dict[str, Any]:
        """Read a global content page after validating admin access and filter parameters."""
        async with sessions() as session:
            return await read_catalogue(
                session,
                page=page,
                page_size=page_size,
                query=query,
                content_type=content_type,
                series_id=series_id,
                status=status,
            )

    return router
