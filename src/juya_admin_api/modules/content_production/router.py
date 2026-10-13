"""Typed, permission-scoped production APIs. Technical versions travel in DTOs."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile

from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.content.router import AdminDependency
from juya_admin_api.modules.content_production.schemas import (
    CandidateDecisionRequest,
    CandidateGroups,
    CommandResult,
    ContentContexts,
    ContextLookupRequest,
    CorrectionRequest,
    EntryConfirmationRequest,
    FromBatchRequest,
    ImportConfirmRequest,
    ImportPreview,
    ItemPage,
    ManualRequest,
    OrderPage,
    OrganizeRequest,
    PublicationConfirmRequest,
    PublicationState,
    SampleActionRequest,
    SamplingState,
    ScopedRequest,
    StageRetryRequest,
    WorkItemDetail,
    WorkOrder,
    WorkOrderUsage,
)
from juya_admin_api.modules.content_production.service import WorkOrderService
from juya_admin_api.shared.errors import AppError

Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=100)]


def create_work_order_router(
    service: WorkOrderService,
    *,
    current_admin: AdminDependency,
    current_admin_write: AdminDependency,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/content-production", tags=["content-production"])
    Reader = Annotated[SessionRecord, Depends(current_admin)]
    Writer = Annotated[SessionRecord, Depends(current_admin_write)]

    @router.post("/contexts", response_model=ContentContexts)
    async def contexts(body: ContextLookupRequest, admin: Reader) -> ContentContexts:
        return await service.contexts(body.content_type, body.content_ids)

    @router.post("/work-orders", response_model=WorkOrder, status_code=202)
    async def upload(
        file: Annotated[UploadFile, File()],
        series_id: Annotated[str, Form()],
        content_type: Annotated[Literal["dialogue", "word_card", "short_reading"], Form()],
        mode: Annotated[Literal["ZIP", "DOCX"], Form()],
        admin: Writer,
        key: Key,
    ) -> WorkOrder:
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > service.imports.limits.max_package_bytes:
                raise AppError("IMPORT_PACKAGE_TOO_LARGE", "文件超过导入上限", 413)
        return await service.upload(
            series_id=series_id,
            content_type=content_type,
            mode=mode,
            data=bytes(data),
            actor=str(admin.admin_user_id),
            key=key,
        )

    @router.post("/work-orders/from-batch", response_model=WorkOrder)
    async def from_batch(body: FromBatchRequest, admin: Writer, key: Key) -> WorkOrder:
        return await service.from_batch(body.batch_id, str(admin.admin_user_id), key)

    @router.post("/work-orders/manual", response_model=WorkOrder)
    async def manual(body: ManualRequest, admin: Writer, key: Key) -> WorkOrder:
        if body.current and body.current.template != body.content_type:
            raise AppError("WORK_CONTENT_TYPE_INVALID", "正文模板与所选类型不一致", 422)
        return await service.manual(
            series_id=body.series_id,
            content_type=body.content_type,
            content_id=body.content_id,
            content=body.current.content.model_dump(mode="json") if body.current else None,
            actor=str(admin.admin_user_id),
            key=key,
        )

    @router.get("/work-orders", response_model=OrderPage)
    async def orders(
        admin: Reader,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        series_id: str | None = None,
    ) -> OrderPage:
        return await service.list_orders(page=page, page_size=page_size, series_id=series_id)

    @router.get("/work-orders/{work_order_id}", response_model=WorkOrder)
    async def get(work_order_id: str, admin: Reader) -> WorkOrder:
        return await service.get(work_order_id)

    @router.get("/work-orders/{work_order_id}/items", response_model=ItemPage)
    async def items(
        work_order_id: str,
        admin: Reader,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        status: Literal[
            "processing", "normal", "review_required", "failed", "published", "cancelled"
        ]
        | None = None,
    ) -> ItemPage:
        return await service.items(work_order_id, page=page, page_size=page_size, status=status)

    @router.get("/work-orders/{work_order_id}/items/{item_id}", response_model=WorkItemDetail)
    async def item(work_order_id: str, item_id: str, admin: Reader) -> WorkItemDetail:
        return await service.item(work_order_id, item_id)

    @router.get(
        "/work-orders/{work_order_id}/items/{item_id}/original", response_model=WorkItemDetail
    )
    async def original(work_order_id: str, item_id: str, admin: Reader) -> WorkItemDetail:
        return await service.item(work_order_id, item_id)

    @router.get("/work-orders/{work_order_id}/original-file")
    async def original_file(work_order_id: str, admin: Reader) -> Response:
        data, mode = await service.original_file(work_order_id)
        return Response(
            data,
            media_type="application/zip"
            if mode in {"ZIP", "MATERIAL_ZIP"}
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": 'attachment; filename="original.'
                + ("zip" if mode in {"ZIP", "MATERIAL_ZIP"} else "docx")
                + '"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/work-orders/{work_order_id}/import-preview", response_model=ImportPreview)
    async def import_preview(work_order_id: str, admin: Reader) -> ImportPreview:
        return await service.import_preview(work_order_id)

    @router.put(
        "/work-orders/{work_order_id}/items/{item_id}/correction", response_model=CommandResult
    )
    async def correct(
        work_order_id: str, item_id: str, body: CorrectionRequest, admin: Writer, key: Key
    ) -> CommandResult:
        detail = await service.item(work_order_id, item_id)
        if detail.current is None or body.current.template != detail.current.template:
            raise AppError("WORK_CONTENT_TYPE_INVALID", "修正模板与当前篇目不一致", 422)
        payload = body.model_dump(exclude={"current"}) | {
            "content": body.current.content.model_dump(mode="json")
        }
        return await service.correct(work_order_id, item_id, payload, str(admin.admin_user_id), key)

    @router.post(
        "/work-orders/{work_order_id}/import-confirm", response_model=CommandResult, status_code=202
    )
    async def import_confirm(
        work_order_id: str, body: ImportConfirmRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "import-confirm", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post(
        "/work-orders/{work_order_id}/import-retry", response_model=CommandResult, status_code=202
    )
    async def import_retry(
        work_order_id: str, body: ImportConfirmRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "import-retry", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post(
        "/work-orders/{work_order_id}/organize", response_model=CommandResult, status_code=202
    )
    async def organize(
        work_order_id: str, body: OrganizeRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "organize", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.get("/work-orders/{work_order_id}/candidates", response_model=CandidateGroups)
    async def candidate_groups(work_order_id: str, admin: Reader) -> CandidateGroups:
        return await service.candidate_groups(work_order_id)

    @router.post("/work-orders/{work_order_id}/candidate-decisions", response_model=CommandResult)
    async def decide_candidates(
        work_order_id: str, body: CandidateDecisionRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id,
            "candidate-decisions",
            body.model_dump(exclude_none=True),
            str(admin.admin_user_id),
            key,
        )

    @router.post("/work-orders/{work_order_id}/entry-confirmations", response_model=CommandResult)
    async def confirm_entries(
        work_order_id: str, body: EntryConfirmationRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "entry-confirmations", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.get("/work-orders/{work_order_id}/usage", response_model=WorkOrderUsage)
    async def work_usage(work_order_id: str, admin: Reader) -> WorkOrderUsage:
        return await service.usage(work_order_id)

    @router.post("/work-orders/{work_order_id}/sampling", response_model=CommandResult)
    async def create_sampling(
        work_order_id: str, body: ScopedRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "sampling", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.get("/work-orders/{work_order_id}/sampling", response_model=SamplingState)
    async def sampling(work_order_id: str, admin: Reader) -> SamplingState:
        return await service.sampling(work_order_id)

    @router.post("/work-orders/{work_order_id}/sample-actions", response_model=CommandResult)
    async def sample_action(
        work_order_id: str, body: SampleActionRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "sample-action", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post("/work-orders/{work_order_id}/publication-preview", response_model=CommandResult)
    async def publication_preview(
        work_order_id: str, body: ScopedRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "publication-preview", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post(
        "/work-orders/{work_order_id}/publication-confirm",
        response_model=CommandResult,
        status_code=202,
    )
    async def publication_confirm(
        work_order_id: str, body: PublicationConfirmRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "publication-confirm", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post(
        "/work-orders/{work_order_id}/publication-retry",
        response_model=CommandResult,
        status_code=202,
    )
    async def publication_retry(
        work_order_id: str, body: PublicationConfirmRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "publication-retry", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.get("/work-orders/{work_order_id}/publication", response_model=PublicationState)
    async def publication(work_order_id: str, admin: Reader) -> PublicationState:
        return await service.publication_state(work_order_id, str(admin.admin_user_id))

    @router.post(
        "/work-orders/{work_order_id}/stage-retry", response_model=CommandResult, status_code=202
    )
    async def stage_retry(
        work_order_id: str, body: StageRetryRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "stage-retry", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post("/work-orders/{work_order_id}/cancel", response_model=CommandResult)
    async def cancel(
        work_order_id: str, body: ScopedRequest, admin: Writer, key: Key
    ) -> CommandResult:
        return await service.command(
            work_order_id, "cancel", body.model_dump(), str(admin.admin_user_id), key
        )

    @router.post(
        "/work-orders/{work_order_id}/commands/{command_id}/resume", response_model=CommandResult
    )
    async def resume(work_order_id: str, command_id: str, admin: Writer) -> CommandResult:
        return await service.resume(work_order_id, command_id, str(admin.admin_user_id))

    return router
