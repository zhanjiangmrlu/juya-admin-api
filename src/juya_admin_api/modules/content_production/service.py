"""Persistent membership and atomic corrections over the existing content stores."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.publication_ranges import PublicationRangeService
from juya_admin_api.modules.content.schemas import SceneContent, normalize_audio_change
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.modules.content_import.admin_stages import StageAdminStore
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.content_production.projection import WorkProjection, counts, issues
from juya_admin_api.modules.content_production.schemas import (
    CandidateGroups,
    CommandResult,
    ContentContext,
    ContentContexts,
    ImportItemResult,
    ImportPreview,
    ImportPreviewItem,
    ItemPage,
    OrderPage,
    PendingCommand,
    PublicationCandidate,
    PublicationItemResult,
    PublicationState,
    SamplingCandidate,
    SamplingState,
    WorkItem,
    WorkItemDetail,
    WorkOrder,
    WorkOrderPhases,
    WorkOrderUsage,
)
from juya_admin_api.modules.short_reading.schemas import ReadingContent
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


class WorkOrderService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        imports: ImportService,
        production: ProductionStore,
        readings: ShortReadingStore,
        proofreading: ProofreadingStore,
        publication: PublicationRangeService | None = None,
        stage_admin: StageAdminStore | None = None,
        candidates: Any = None,
        authoring: Any = None,
    ):
        self.sessions, self.imports, self.production = sessions, imports, production
        self.readings, self.proofreading = readings, proofreading
        self.publication, self.stage_admin = publication, stage_admin
        from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
        from juya_admin_api.modules.content.v15_candidates import CandidateService

        self.candidates = candidates or CandidateService(sessions, production, imports.stages)
        self.authoring = authoring or SceneAuthoringStore(
            sessions, proofreading, imports.stages, require_review=proofreading.require_review
        )
        self.projection = WorkProjection(sessions, proofreading)

    async def row(
        self, session: AsyncSession, identity: str, *, lock: bool = False
    ) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT w.*,s.title AS series_title FROM content_work_order w JOIN "
                        "content_series s ON s.public_id=w.series_id WHERE w.public_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": identity},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("WORK_ORDER_NOT_FOUND", "生产工作单不存在", 404)
        return dict(row)

    @staticmethod
    def check_version(order: dict[str, Any], expected: int) -> None:
        if order["version"] != expected:
            raise AppError("WORK_ORDER_VERSION_CONFLICT", "工作单范围已更新,请刷新", 409)
        if order["active_command_id"]:
            raise AppError(
                "WORK_ORDER_COMMAND_PENDING", "工作单有待恢复命令,请使用原幂等键重试", 409
            )
        if order["status"] == "CANCELLED":
            raise AppError("WORK_ORDER_CANCELLED", "工作单已取消", 409)

    async def get(self, identity: str) -> WorkOrder:
        async with self.sessions() as session:
            order = await self.row(session, identity)
            pending = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id AS command_id,action,created_at,lease_until AS "
                            "retry_at "
                            "FROM content_work_command WHERE work_order_id=:id AND "
                            "status='PENDING' ORDER BY id"
                        ),
                        {"id": identity},
                    )
                )
                .mappings()
                .all()
            )
        items = await self.projection.all(order)
        actions = ["sampling", "publication-preview", "correct", "organize", "stage-retry"]
        if order["content_type"] != "short_reading":
            actions.extend(["candidate-decisions", "candidate-dismissals", "entry-confirmations"])
        if order["import_batch_id"] and order["source_mode"] != "MATERIAL_ZIP":
            actions.extend(["import-confirm", "import-retry", "cancel"])
        return WorkOrder(
            work_order_id=identity,
            import_batch_id=order["import_batch_id"],
            source_mode=order["source_mode"],
            series_id=order["series_id"],
            series_title=order["series_title"],
            content_type=order["content_type"],
            version=order["version"],
            status=order["status"],
            created_at=order["created_at"],
            updated_at=order["updated_at"],
            counts=counts(items),
            available_actions=actions if order["status"] != "CANCELLED" else [],
            proofreading_set_id=order["proofreading_set_id"],
            publication_token=order["publication_token"],
            pending_commands=[PendingCommand.model_validate(p) for p in pending],
        )

    async def list_orders(
        self, *, page: int = 1, page_size: int = 20, series_id: str | None = None
    ) -> OrderPage:
        self.validate_page(page, page_size)
        where = " WHERE series_id=:series" if series_id else ""
        params = {"series": series_id, "limit": page_size, "offset": (page - 1) * page_size}
        async with self.sessions() as session:
            total = int(
                await session.scalar(
                    text("SELECT COUNT(*) FROM content_work_order" + where), params
                )
                or 0
            )
            ids: Any = (
                (
                    await session.execute(
                        text(
                            "SELECT public_id FROM content_work_order"
                            + where
                            + " ORDER BY id DESC LIMIT :limit OFFSET :offset"
                        ),
                        params,
                    )
                )
                .scalars()
                .all()
            )
        return OrderPage(
            items=[await self.get(i) for i in ids], total=total, page=page, page_size=page_size
        )

    @staticmethod
    def validate_page(page: int, page_size: int) -> None:
        if page < 1 or not 1 <= page_size <= 100:
            raise AppError("WORK_PAGE_INVALID", "分页参数无效", 422)

    async def items(
        self, identity: str, *, page: int = 1, page_size: int = 20, status: str | None = None
    ) -> ItemPage:
        self.validate_page(page, page_size)
        async with self.sessions() as session:
            order = await self.row(session, identity)
        all_items = await self.projection.all(order)
        filtered = [
            item for item in all_items if status is None or item.production_status == status
        ]
        return ItemPage(
            items=[
                WorkItem.model_validate(
                    i.model_dump(
                        exclude={
                            "current",
                            "source_available",
                            "source_snapshot",
                            "historical_issues",
                            "phase_checks",
                        }
                    )
                )
                for i in filtered[(page - 1) * page_size : page * page_size]
            ],
            total=len(filtered),
            page=page,
            page_size=page_size,
            counts=counts(all_items),
            version=order["version"],
            collection_hash=snapshot_hash(
                [
                    item.model_dump(exclude={"current", "source_snapshot", "historical_issues"})
                    for item in all_items
                ]
            ),
        )

    async def item(self, identity: str, item_id: str) -> WorkItemDetail:
        async with self.sessions() as session:
            order = await self.row(session, identity)
        result = next(
            (
                i
                for i in await self.projection.all(order, original_item_id=item_id)
                if i.item_id == item_id
            ),
            None,
        )
        if result is None:
            raise AppError("WORK_ITEM_NOT_FOUND", "篇目不属于此工作单", 404)
        return result

    async def upload(
        self, *, series_id: str, content_type: str, mode: str, data: bytes, actor: str, key: str
    ) -> WorkOrder:
        created = await self.imports.create(
            series_id=series_id,
            content_type=content_type,
            mode=mode,
            data=data,
            actor=actor,
            key=key,
        )
        return await self.from_batch(created["batch_id"], actor, snapshot_hash(["upload", key]))

    async def from_batch(self, batch_id: str, actor: str, key: str) -> WorkOrder:
        digest = snapshot_hash(batch_id)
        async with self.sessions() as session, session.begin():
            # Lock the batch before registration; one order per batch across actors/keys.
            batch = await self.imports.batch(session, batch_id, True, preview_hash_only=True)
            replay = await self.production.creation_replay(
                session, "work-order.from-batch", actor, key, digest
            )
            identity = (
                replay["work_order_id"]
                if replay
                else await session.scalar(
                    text("SELECT public_id FROM content_work_order WHERE import_batch_id=:id"),
                    {"id": batch_id},
                )
            )
            if not identity:
                identity = new_ulid(datetime.now(UTC))
                await session.execute(
                    text(
                        "INSERT INTO content_work_order(public_id,import_batch_id,source_mode,ser"
                        "ies_id,content_type,created_by) VALUES(:id,:batch,:mode,:series,:type,:a"
                        "ctor)"
                    ),
                    {
                        "id": identity,
                        "batch": batch_id,
                        "mode": batch["import_mode"],
                        "series": batch["series_id"],
                        "type": batch["content_type"],
                        "actor": actor,
                    },
                )
            if replay is None:
                await self.production.remember_creation(
                    session,
                    "work-order.from-batch",
                    actor,
                    key,
                    digest,
                    {"work_order_id": identity},
                    datetime.now(UTC),
                )
        return await self.get(identity)

    async def manual(
        self,
        *,
        series_id: str,
        content_type: str,
        content_id: str | None,
        content: dict[str, Any] | None,
        actor: str,
        key: str,
    ) -> WorkOrder:
        # Delegates own durable keys; an interrupted registration reuses the created content.
        digest = snapshot_hash([series_id, content_type, content_id, content])
        scope = "work-order.manual"
        async with self.sessions() as session:
            replay = await self.production.creation_replay(session, scope, actor, key, digest)
        if replay:
            return await self.get(replay["work_order_id"])
        execution_key = snapshot_hash([scope, actor, key])
        if content_type not in {"dialogue", "word_card", "short_reading"}:
            raise AppError("WORK_CONTENT_TYPE_INVALID", "内容类型无效", 422)
        if content_id is None:
            if content_type == "short_reading":
                data = ReadingContent.model_validate(content or {}).model_dump(mode="json")
                created = await self.readings.command(
                    "create", "new", actor, execution_key, {"series_id": series_id, "content": data}
                )
                content_id = created["content_id"]
            else:
                # Initial save only accepts the creation's original edit version.
                content_id = await self.production.create_scene(
                    series_id, content_type, actor=actor, key=execution_key
                )
                if content:
                    async with self.sessions() as session, session.begin():
                        prior = await self.production.creation_replay(
                            session, "work-order.initial-save", actor, execution_key, digest
                        )
                        if prior is None:
                            revision_id = await session.scalar(
                                text(
                                    "SELECT r.public_id FROM scene s JOIN scene_revision r ON "
                                    "r.id=s.draft_revision_id WHERE s.public_id=:id FOR UPDATE"
                                ),
                                {"id": content_id},
                            )
                            revision = SceneRevision(
                                revision_id,
                                content_id,
                                None,
                                content=SceneContent.model_validate(content).model_dump(),
                                created_by=actor,
                            )
                            await self.production.save_revision_within(session, revision, 1)
                            await self.production.remember_creation(
                                session,
                                "work-order.initial-save",
                                actor,
                                execution_key,
                                digest,
                                {"revision_id": revision_id},
                                datetime.now(UTC),
                            )
        async with self.sessions() as session, session.begin():
            # Series serializes creation receipts, preventing duplicate manual orders.
            await session.execute(
                text("SELECT id FROM content_series WHERE public_id=:id FOR UPDATE"),
                {"id": series_id},
            )
            replay = await self.production.creation_replay(session, scope, actor, key, digest)
            if replay:
                identity = replay["work_order_id"]
            else:
                if content_type == "short_reading":
                    row = await self.readings.row(session, content_id, True)
                    belongs = row["series_id"] == series_id
                    revision_id = row["draft_revision_id"]
                else:
                    scene_row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT cs.public_id AS series_id,t.template_type,r.public_id"
                                    " AS revision_id FROM scene s JOIN content_series cs ON "
                                    "cs.id=s.series_id JOIN content_template t ON "
                                    "t.id=s.template_id LEFT JOIN scene_revision r ON "
                                    "r.id=s.draft_revision_id WHERE s.public_id=:id FOR UPDATE"
                                ),
                                {"id": content_id},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    belongs = bool(
                        scene_row
                        and scene_row["series_id"] == series_id
                        and (
                            "word_card"
                            if scene_row["template_type"] == "vocabulary"
                            else scene_row["template_type"]
                        )
                        == content_type
                    )
                    revision_id = scene_row["revision_id"] if scene_row else None
                if not belongs or not revision_id:
                    raise AppError(
                        "WORK_CONTENT_SCOPE_INVALID", "内容类型、分类或当前草稿不匹配", 409
                    )
                identity, item_id = new_ulid(datetime.now(UTC)), new_ulid(datetime.now(UTC))
                await session.execute(
                    text(
                        "INSERT INTO content_work_order(public_id,source_mode,series_id,content_t"
                        "ype,created_by) VALUES(:id,'MANUAL',:series,:type,:actor)"
                    ),
                    {"id": identity, "series": series_id, "type": content_type, "actor": actor},
                )
                await session.execute(
                    text(
                        "INSERT INTO content_work_item(public_id,work_order_id,position,content_i"
                        "d,original_revision_id) VALUES(:id,:order,1,:content,:revision)"
                    ),
                    {
                        "id": item_id,
                        "order": identity,
                        "content": content_id,
                        "revision": revision_id,
                    },
                )
                await self.production.remember_creation(
                    session,
                    scope,
                    actor,
                    key,
                    digest,
                    {"work_order_id": identity},
                    datetime.now(UTC),
                )
        return await self.get(identity)

    async def import_preview(self, identity: str) -> ImportPreview:
        async with self.sessions() as session:
            order = await self.row(session, identity)
        if not order["import_batch_id"]:
            raise AppError("WORK_IMPORT_NOT_FOUND", "手工工作单没有导入预览", 404)
        if order["source_mode"] == "MATERIAL_ZIP":
            raise AppError("WORK_MATERIAL_USE_BINDINGS", "素材包请使用原素材绑定预览入口", 409)
        result = await self.imports.preview(order["import_batch_id"])
        items = await self.projection.all(order, include_originals=True)
        lookup = {i.import_item_id: i for i in items}
        return ImportPreview(
            work_order_id=identity,
            batch_id=result["batch_id"],
            status=result["status"],
            preview_hash=result["preview_hash"],
            preview_version=result["preview_version"],
            batch_issues=issues(result["batch_issues"]),
            unreferenced_files=result["manifest"].get("unreferenced_files", []),
            items=[
                ImportPreviewItem(
                    item_id=lookup[i["item_id"]].item_id,
                    import_item_id=i["item_id"],
                    import_content_id=i["import_content_id"],
                    row_no=i["row_no"],
                    operation=i["operation"],
                    status=i["status"],
                    candidate=i["candidate"],
                    issues=issues(i["issues"], source_missing=not lookup[i["item_id"]].content_id),
                    defaults=i["defaults"],
                    resources=i["resources"],
                    entries=i["entries"],
                    source_snapshot=lookup[i["item_id"]].source_snapshot,
                    result=ImportItemResult.model_validate(
                        {k: v for k, v in i["result"].items() if k in ImportItemResult.model_fields}
                    )
                    if i.get("result")
                    else None,
                )
                for i in result["items"]
            ],
        )

    async def original_file(self, identity: str) -> tuple[bytes, str]:
        async with self.sessions() as session:
            order = await self.row(session, identity)
            if not order["import_batch_id"]:
                raise AppError("WORK_ORIGINAL_UNAVAILABLE", "没有上传的原文件", 404)
            batch = await self.imports.batch(
                session, order["import_batch_id"], preview_hash_only=True
            )
        return await self.imports.original(batch), batch["import_mode"]

    async def phases(self, identity: str) -> WorkOrderPhases:
        from .phases import read

        return await read(self, identity)

    async def correct(
        self, identity: str, item_id: str, payload: dict[str, Any], actor: str, key: str
    ) -> CommandResult:
        # Existing strict models keep parser-only evidence out of drafts.
        request_hash = snapshot_hash([item_id, payload])
        scope = "work.correct:" + identity
        async with self.sessions() as session, session.begin():
            order = await self.row(session, identity, lock=True)
            replay = await self.production.creation_replay(session, scope, actor, key, request_hash)
            if replay:
                return CommandResult.model_validate(replay)
            self.check_version(order, payload["expected_version"])
            item = (
                await session.execute(
                    text(
                        "SELECT content_id FROM content_work_item WHERE public_id=:item AND "
                        "work_order_id=:order AND cancelled=0 FOR UPDATE"
                    ),
                    {"item": item_id, "order": identity},
                )
            ).first()
            if not item:
                raise AppError("WORK_ITEM_NOT_FOUND", "篇目不属于此工作单", 404)
            if not item.content_id:
                raise AppError(
                    "WORK_SOURCE_CORRECTION_REQUIRED", "此行尚未导入,请修正或补充原文件", 409
                )
            revision_id = payload["expected_revision_id"]
            table = (
                "short_reading_revision"
                if order["content_type"] == "short_reading"
                else "scene_revision"
            )
            if order["content_type"] == "short_reading":
                reading = await self.readings.row(session, item.content_id, True)
                if (
                    reading["draft_revision_id"] != revision_id
                    or reading["version"] != payload["expected_content_version"]
                ):
                    raise AppError("WORK_ITEM_VERSION_CONFLICT", "当前候选已更新", 409)
                previous = await self.readings.revision(session, item.content_id, revision_id, True)
                prior_content = previous["content_snapshot"]
            else:
                actual = (
                    await session.execute(
                        text(
                            "SELECT r.public_id,r.edit_version FROM scene s JOIN scene_revision "
                            "r ON r.id=s.draft_revision_id WHERE s.public_id=:id FOR UPDATE"
                        ),
                        {"id": item.content_id},
                    )
                ).first()
                if (
                    not actual
                    or actual.public_id != revision_id
                    or actual.edit_version != payload["expected_edit_version"]
                    or actual.edit_version != payload["expected_content_version"]
                ):
                    raise AppError("WORK_ITEM_VERSION_CONFLICT", "当前候选已更新", 409)
                prior_content = await read_json(
                    session, table, "content_snapshot", {"public_id": revision_id}
                )
            if snapshot_hash(prior_content) != payload["candidate_hash"]:
                raise AppError("WORK_ITEM_VERSION_CONFLICT", "候选正文已更新", 409)
            if order["content_type"] == "short_reading":
                content = ReadingContent.model_validate(payload["content"])
                before = ReadingContent.model_validate(prior_content)
                # Saving text/audio cannot retain a stale human listening approval.
                if content.audio and (
                    before.audio != content.audio or before.paragraphs_en != content.paragraphs_en
                ):
                    content.audio.listening_confirmed = False
                    content.audio.confirmed_asset_id = None
                    content.audio.confirmed_version_id = None
                await self.readings.mutate(
                    session,
                    "save",
                    reading,
                    actor,
                    {
                        "revision_id": revision_id,
                        "expected_edit_version": payload["expected_edit_version"],
                        "content": content.model_dump(mode="json"),
                    },
                )
                await self.proofreading.invalidate_within(session, revision_id)
            else:
                proposed = normalize_audio_change(
                    SceneContent.model_validate(prior_content),
                    SceneContent.model_validate(payload["content"]),
                )
                await self.production.save_revision_within(
                    session,
                    SceneRevision(
                        revision_id,
                        item.content_id,
                        None,
                        content=proposed.model_dump(mode="json"),
                        created_by=actor,
                    ),
                    payload["expected_edit_version"],
                )
            saved = await read_json(session, table, "content_snapshot", {"public_id": revision_id})
            digest = snapshot_hash(saved)
            original = await read_json(
                session, "content_work_item", "source_snapshot", {"public_id": item_id}
            )
            historical_raw = (
                await read_json(
                    session, "content_work_item", "historical_issues", {"public_id": item_id}
                )
                or []
            )
            historical = issues(historical_raw)
            from .effective_issues import dependency_evidence
            from .effective_issues import issue_id as original_issue_id

            historical_by_id = {original_issue_id(issue): issue for issue in historical_raw}
            resolved = payload.get("resolved_issue_ids", [])
            if resolved and (
                not original
                or payload.get("original_compared") is not True
                or set(resolved) - {i.id for i in historical}
            ):
                raise AppError("WORK_ISSUE_ACK_INVALID", "请对照原始输入确认此篇问题", 422)
            for issue_id in resolved:
                await session.execute(
                    text(
                        "INSERT IGNORE INTO content_work_issue_resolution(work_item_id,issue_id,s"
                        "ource_hash,candidate_hash,resolved_by"
                        ",dependency_hash,dependency_evidence) "
                        "VALUES(:item,:issue,:source,:hash,:actor,:dependency,:evidence)"
                    ),
                    {
                        "item": item_id,
                        "issue": issue_id,
                        "source": snapshot_hash(original),
                        "dependency": snapshot_hash(
                            dependency_evidence(saved, historical_by_id[issue_id])
                        ),
                        "evidence": json.dumps(
                            dependency_evidence(saved, historical_by_id[issue_id])
                        ),
                        "hash": digest,
                        "actor": actor,
                    },
                )
            result = CommandResult(
                command_id=new_ulid(datetime.now(UTC)),
                work_order_id=identity,
                action="correct",
                status="SUCCEEDED",
                version=order["version"],
                selected_item_ids=[item_id],
                revision_id=revision_id,
                candidate_hash=digest,
                edit_version=int(
                    await session.scalar(
                        text(f"SELECT edit_version FROM {table} WHERE public_id=:id"),
                        {"id": revision_id},
                    )
                    or 1
                ),
            )
            await self.proofreading.audit(
                session,
                identity,
                actor,
                "work.correct",
                result.model_dump()
                | {"resolved_issue_ids": resolved, "source_hash": snapshot_hash(original)},
            )
            await self.production.remember_creation(
                session, scope, actor, key, request_hash, result.model_dump(), datetime.now(UTC)
            )
        return result

    async def command(
        self, identity: str, action: str, payload: dict[str, Any], actor: str, key: str
    ) -> CommandResult:
        from juya_admin_api.modules.content_production.commands import execute

        return await execute(self, identity, action, payload, actor, key)

    async def resume(self, identity: str, command_id: str, actor: str) -> CommandResult:
        async with self.sessions() as session:
            await self.row(session, identity)
            receipt = (
                await session.execute(
                    text(
                        "SELECT action,idempotency_key FROM content_work_command "
                        "WHERE public_id=:command AND work_order_id=:order AND actor_id=:actor"
                    ),
                    {"command": command_id, "order": identity, "actor": actor},
                )
            ).first()
            if receipt is None:
                raise AppError("WORK_COMMAND_NOT_FOUND", "命令不属于当前管理员和工作单", 404)
            request = await read_json(
                session, "content_work_command", "request", {"public_id": command_id}
            )
        return await self.command(
            identity, receipt.action, request["payload"], actor, receipt.idempotency_key
        )

    async def sampling(self, identity: str) -> SamplingState:
        async with self.sessions() as session:
            order = await self.row(session, identity)
        if not order["proofreading_set_id"]:
            raise AppError("WORK_SAMPLING_NOT_FOUND", "尚未创建抽样集合", 404)
        state = await self.proofreading.get(order["proofreading_set_id"])
        return SamplingState(
            work_order_id=identity,
            proofreading_set_id=state["public_id"],
            collection_hash=state["collection_hash"],
            status=state["status"],
            normal_count=sum(
                bool(c.get("normal") and not c.get("failure")) for c in state["candidates"]
            ),
            sample_ids=state["sample_ids"],
            confirmed_revision_ids=state["confirmed_ids"],
            candidates=[
                SamplingCandidate.model_validate(
                    {k: v for k, v in c.items() if k in SamplingCandidate.model_fields}
                )
                for c in state["candidates"]
            ],
        )

    async def publication_state(self, identity: str, actor: str) -> PublicationState:
        async with self.sessions() as session:
            order = await self.row(session, identity)
        if not order["publication_token"] or self.publication is None:
            raise AppError("WORK_PUBLICATION_NOT_FOUND", "没有可恢复的发布范围", 404)
        state = await self.publication.recover(order["publication_token"], actor)
        return PublicationState(
            work_order_id=identity,
            token=state["token"],
            collection_hash=state["collection_hash"],
            version=state["version"],
            status=state["status"],
            stage_id=state["stage_id"],
            expires_at=state["expires_at"],
            candidates=[PublicationCandidate.model_validate(c) for c in state["candidates"]],
            results=[
                PublicationItemResult(
                    revision_id=r, status=v["status"], error_code=v.get("error_code")
                )
                for r, v in state["results"]["items"].items()
            ],
        )

    async def contexts(self, content_type: str, content_ids: list[str]) -> ContentContexts:
        if not content_ids or len(content_ids) > 100:
            raise AppError("WORK_CONTEXT_SCOPE_INVALID", "列表上下文最多查询100项", 422)
        from sqlalchemy import bindparam

        async with self.sessions() as session:
            rows: Any = (
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT w.work_order_id FROM content_work_item w JOIN "
                            "content_work_order o ON o.public_id=w.work_order_id WHERE "
                            "o.content_type=:type AND w.content_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"type": content_type, "ids": content_ids},
                    )
                )
                .scalars()
                .all()
            )
        result: list[ContentContext] = []
        for order_id in rows:
            async with self.sessions() as session:
                order = await self.row(session, order_id)
            result.extend(
                ContentContext(
                    content_id=i.content_id,
                    work_order_id=order_id,
                    work_item_id=i.item_id,
                    production_status=i.production_status,
                    issue_count=len(i.issues),
                )
                for i in await self.projection.all(order)
                if i.content_id is not None and i.content_id in content_ids
            )
        return ContentContexts(items=result)

    async def candidate_groups(self, identity: str) -> CandidateGroups:
        from .candidates import groups

        return await groups(self, identity)

    async def usage(self, identity: str) -> WorkOrderUsage:
        from .usage import usage

        return await usage(self, identity)
