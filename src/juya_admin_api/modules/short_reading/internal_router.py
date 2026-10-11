"""Authorized internal access. The BFF adapter supplies signed identity and live policy."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError

ContextDependency = Callable[..., Awaitable[dict[str, Any]]]
Authorizer = Callable[[AsyncSession, str, dict[str, Any]], Awaitable[bool]]
ResourceSigner = Callable[[AsyncSession, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


def create_short_reading_internal_router(
    store: ShortReadingStore,
    *,
    current_context: ContextDependency,
    authorizer: Authorizer,
    resource_signer: ResourceSigner,
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/short-readings", tags=["internal-short-readings"])

    async def require(session: AsyncSession, identity: str, context: dict[str, Any]) -> None:
        if not await authorizer(session, identity, {**context, "purpose": "PREVIEW"}):
            raise AppError("CONTENT_ACCESS_DENIED", "当前账号暂未开通此内容", 403)

    @router.get("")
    async def listing(
        response: Response, context: Annotated[dict[str, Any], Depends(current_context)]
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        async with store.sessions() as session:
            ids: Any = (
                await session.execute(
                    text(
                        "SELECT public_id FROM short_reading "
                        "WHERE status='PUBLISHED' ORDER BY public_id"
                    )
                )
            ).scalars()
            items = []
            for identity in ids:
                if await authorizer(session, identity, {**context, "purpose": "PREVIEW"}):
                    detail = await store.published_detail_within(session, identity)
                    items.append(
                        detail.model_dump(
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
                            }
                        )
                    )
            return {"items": items}

    @router.get("/{identity}/preview")
    async def preview(
        identity: str,
        response: Response,
        context: Annotated[dict[str, Any], Depends(current_context)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        async with store.sessions() as session:
            # Adapter decides visibility as well as the live module gate for PREVIEW purpose.
            await require(session, identity, {**context, "purpose": "PREVIEW"})
            detail = await store.published_detail_within(session, identity)
            return detail.model_dump(
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
                }
            )

    @router.post("/{identity}/open")
    async def open_reading(
        identity: str,
        response: Response,
        context: Annotated[dict[str, Any], Depends(current_context)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        async with store.sessions() as session:
            await require(session, identity, context)
            return (await store.published_detail_within(session, identity)).model_dump(mode="json")

    @router.get("/{identity}/resources/{resource_id}/signed-url")
    async def resource(
        identity: str,
        resource_id: str,
        response: Response,
        context: Annotated[dict[str, Any], Depends(current_context)],
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        async with store.sessions() as session:
            await require(session, identity, context)
            detail = await store.published_detail_within(session, identity)
            audio = detail.whole_audio
            if audio is None or resource_id != audio.target_id:
                raise AppError("RESOURCE_NOT_FOUND", "短文资源不存在", 404)
            return await resource_signer(session, audio.target_id, audio.resource_version, context)

    return router
