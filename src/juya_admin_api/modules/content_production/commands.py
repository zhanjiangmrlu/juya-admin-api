"""Freeze server scope, release locks, delegate, then attach durable results.

A pending receipt blocks conflicting scope commands. Unknown delegated outcomes retain
the reservation; retrying the same key after the lease recovers the same frozen input.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content.publication_ranges import digest as publication_digest
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_auto_audio import schedule_audio_within
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.content_import.parser import canonical_hash
from juya_admin_api.modules.content_production.projection import select_items
from juya_admin_api.modules.content_production.schemas import CommandResult
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

if TYPE_CHECKING:
    from juya_admin_api.modules.content_production.service import WorkOrderService

SUPPORTED = {
    "import-confirm",
    "import-retry",
    "sampling",
    "sample-action",
    "publication-preview",
    "publication-confirm",
    "publication-retry",
    "stage-retry",
    "organize",
    "cancel",
}


async def execute(
    service: "WorkOrderService",
    identity: str,
    action: str,
    payload: dict[str, Any],
    actor: str,
    key: str,
) -> CommandResult:
    if action not in SUPPORTED or not key or len(key) > 100:
        raise AppError("WORK_COMMAND_INVALID", "生产命令或幂等键无效", 422)
    digest = snapshot_hash([action, payload])
    async with service.sessions() as session:
        order = await service.row(session, identity)
    all_items = await service.projection.all(order)
    selected = select_items(all_items, payload)
    if action == "import-retry":
        selected = [
            i
            for i in selected
            if i.production_status == "failed" and not i.content_id and i.import_item_id
        ]
    if action == "publication-preview":
        selected = [i for i in selected if i.publishable]
    frozen = [
        {
            k: v
            for k, v in i.model_dump(mode="json").items()
            if k
            in {
                "item_id",
                "import_item_id",
                "content_id",
                "revision_id",
                "candidate_hash",
                "edit_version",
                "content_version",
                "tasks",
                "publishable",
                "production_status",
            }
        }
        | {"current": i.current is not None}
        for i in selected
    ]
    now = datetime.now(UTC).replace(tzinfo=None)
    async with service.sessions() as session, session.begin():
        order = await service.row(session, identity, lock=True)
        receipt = (
            (
                await session.execute(
                    text(
                        "SELECT public_id,status,request_hash,lease_until,fencing_version FROM "
                        "content_work_command WHERE work_order_id=:order AND actor_id=:actor AND "
                        "idempotency_key=:key FOR UPDATE"
                    ),
                    {"order": identity, "actor": actor, "key": key},
                )
            )
            .mappings()
            .first()
        )
        if receipt:
            if receipt["request_hash"] != digest:
                raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已用于不同请求", 409)
            if receipt["status"] == "SUCCEEDED":
                return CommandResult.model_validate(
                    await read_json(
                        session,
                        "content_work_command",
                        "result",
                        {"public_id": receipt["public_id"]},
                    )
                )
            if receipt["status"] == "FAILED":
                error = await read_json(
                    session, "content_work_command", "result", {"public_id": receipt["public_id"]}
                )
                raise AppError(error["code"], error["message"], error["status"])
            if receipt["lease_until"] > now:
                raise AppError("WORK_COMMAND_RUNNING", "命令正在执行,稍后使用相同幂等键恢复", 409)
            command_id = receipt["public_id"]
            request = await read_json(
                session, "content_work_command", "request", {"public_id": command_id}
            )
            if (
                action in {"publication-confirm", "publication-retry"}
                and "selected_revision_ids" not in request
            ):
                # Recover pre-fix reservations from an already committed exact delegate receipt.
                await freeze_publication_request(
                    service, session, action, request, actor, command_id
                )
                await session.execute(
                    text("UPDATE content_work_command SET request=:request WHERE public_id=:id"),
                    {"request": encode(request), "id": command_id},
                )
            fence = receipt["fencing_version"] + 1
            await session.execute(
                text(
                    "UPDATE content_work_command SET lease_until=:until,fencing_version=:fence "
                    "WHERE public_id=:id"
                ),
                {"until": now + timedelta(minutes=5), "fence": fence, "id": command_id},
            )
        else:
            service.check_version(order, payload["expected_version"])
            if not selected and action not in {"sampling", "cancel", "stage-retry"}:
                raise AppError("WORK_SELECTION_EMPTY", "此范围没有符合条件的篇目", 409)
            command_id = new_ulid(datetime.now(UTC))
            request = {
                "payload": payload,
                "items": frozen,
                "order": {
                    k: order[k]
                    for k in (
                        "public_id",
                        "import_batch_id",
                        "content_type",
                        "proofreading_set_id",
                        "publication_token",
                        "version",
                    )
                },
                "delegate_id": new_ulid(datetime.now(UTC)),
            }
            if action in {"publication-confirm", "publication-retry"}:
                await freeze_publication_request(
                    service, session, action, request, actor, command_id
                )
            fence = 1
            await session.execute(
                text(
                    "INSERT INTO content_work_command(public_id,work_order_id,actor_id,idempotenc"
                    "y_key,action,request_hash,request,lease_until) "
                    "VALUES(:id,:order,:actor,:key,:action,:hash,:request,:until)"
                ),
                {
                    "id": command_id,
                    "order": identity,
                    "actor": actor,
                    "key": key,
                    "action": action,
                    "hash": digest,
                    "request": encode(request),
                    "until": now + timedelta(minutes=5),
                },
            )
            await session.execute(
                text(
                    "UPDATE content_work_order SET active_command_id=:command WHERE public_id=:id"
                ),
                {"command": command_id, "id": identity},
            )
    # All delegate sessions start after the reservation transaction has committed.
    try:
        result, refs = await delegate(service, identity, command_id, action, request, actor)
    except AppError as error:
        async with service.sessions() as session, session.begin():
            changed = await session.execute(
                text(
                    "UPDATE content_work_command SET status='FAILED',result=:result WHERE "
                    "public_id=:id AND fencing_version=:fence"
                ),
                {
                    "id": command_id,
                    "fence": fence,
                    "result": encode(
                        {"code": error.code, "message": error.message, "status": error.status_code}
                    ),
                },
            )
            if getattr(changed, "rowcount", 0):
                await session.execute(
                    text(
                        "UPDATE content_work_order SET active_command_id=NULL WHERE "
                        "public_id=:id AND active_command_id=:command"
                    ),
                    {"id": identity, "command": command_id},
                )
        raise
    async with service.sessions() as session, session.begin():
        order = await service.row(session, identity, lock=True)
        current = (
            await session.execute(
                text(
                    "SELECT fencing_version,status FROM content_work_command WHERE public_id=:id "
                    "FOR UPDATE"
                ),
                {"id": command_id},
            )
        ).first()
        if current is None or current.fencing_version != fence or current.status != "PENDING":
            raise AppError("WORK_COMMAND_LEASE_LOST", "命令恢复租约已变化,请刷新", 409)
        version = order["version"] + 1
        result.version = version
        await session.execute(
            text(
                "UPDATE content_work_order SET version=:version,active_command_id=NULL,updated_at"
                "=UTC_TIMESTAMP(6),proofreading_set_id=COALESCE(:proof,proofreading_set_id),publi"
                "cation_token=COALESCE(:pub,publication_token),status=:status WHERE public_id=:id"
            ),
            {
                "id": identity,
                "version": version,
                "proof": refs.get("proofreading_set_id"),
                "pub": refs.get("publication_token"),
                "status": refs.get("status", order["status"]),
            },
        )
        await session.execute(
            text(
                "UPDATE content_work_command SET status='SUCCEEDED',result=:result WHERE "
                "public_id=:id"
            ),
            {"id": command_id, "result": encode(result.model_dump())},
        )
        await service.proofreading.audit(
            session, identity, actor, "work." + action, result.model_dump()
        )
    return result


async def delegate(
    service: "WorkOrderService",
    identity: str,
    command_id: str,
    action: str,
    request: dict[str, Any],
    actor: str,
) -> tuple[CommandResult, dict[str, Any]]:
    payload, items, order = request["payload"], request["items"], request["order"]
    key = snapshot_hash(["work-command", command_id])
    result = CommandResult(
        command_id=command_id,
        work_order_id=identity,
        action=action,
        status="SUCCEEDED",
        version=order["version"],
        selected_item_ids=[i["item_id"] for i in items],
    )
    refs: dict[str, Any] = {}
    if action in {"import-confirm", "import-retry"}:
        if not order["import_batch_id"] or any(not i["import_item_id"] for i in items):
            raise AppError("WORK_IMPORT_SCOPE_INVALID", "此范围不是导入篇目", 409)
        confirmation = {
            k: payload[k]
            for k in (
                "preview_hash",
                "preview_version",
                "acknowledged_defaults",
                "confirmed_conflicts",
            )
        }
        confirmation["selected"] = [i["import_item_id"] for i in items]
        async with service.sessions() as session:
            replay = await service.production.creation_replay(
                session,
                "v15.import.enqueue",
                actor,
                key,
                canonical_hash([order["import_batch_id"], confirmation]),
                lock=False,
            )
        if replay is not None:
            result.stage_id, result.status = replay["stage_id"], "QUEUED"
            return result, refs
        # Recomputing a preview after a partial import changes successful rows' mapping.
        # Recovery must consume the already frozen original preview, never regenerate it.
        async with service.sessions() as session:
            batch = await service.imports.batch(session, order["import_batch_id"])
        preview = batch["manifest"].get("preview", {})
        if (
            preview.get("hash") != payload["preview_hash"]
            or batch["preview_version"] != payload["preview_version"]
        ):
            raise AppError("IMPORT_PREVIEW_CHANGED", "请重新检查导入预览", 409)
        for item in preview["items"]:
            if item["item_id"] not in confirmation["selected"]:
                continue
            if action == "import-retry":
                async with service.sessions() as session:
                    current_status = await session.scalar(
                        text("SELECT status FROM content_import_item WHERE public_id=:id"),
                        {"id": item["item_id"]},
                    )
                if current_status != "FAILED":
                    raise AppError("WORK_IMPORT_RETRY_STALE", "仅能重试仍然失败的导入行", 409)
            if set(item["defaults"]) - set(payload["acknowledged_defaults"]):
                raise AppError("IMPORT_DEFAULT_CONFIRMATION_REQUIRED", "请确认默认素材处理", 409)
            if any(
                i.get("severity") == "CONFIRM"
                and f"{item['item_id']}:{i['field']}" not in payload["confirmed_conflicts"]
                for i in item["issues"]
            ):
                raise AppError(
                    "IMPORT_TEXT_CONFIRMATION_REQUIRED", "请确认原文与表格冲突两侧值", 409
                )
        delegated = await service.imports.queue_confirmation(
            order["import_batch_id"], confirmation, actor, key
        )
        result.stage_id, result.status = delegated["stage_id"], "QUEUED"
    elif action == "sampling":
        candidates = candidate_scope(items, order["content_type"])
        state = await service.proofreading.create(candidates, actor, set_id=request["delegate_id"])
        result.proofreading_set_id = state["public_id"]
        refs["proofreading_set_id"] = state["public_id"]
    elif action == "sample-action":
        set_id = order["proofreading_set_id"]
        if not set_id:
            raise AppError("WORK_SAMPLING_NOT_FOUND", "尚未创建抽样集合", 404)
        state = await service.proofreading.get(set_id)
        revisions = [i["revision_id"] for i in items]
        if not set(revisions) <= {c["revision_id"] for c in state["candidates"]}:
            raise AppError("WORK_SAMPLING_SCOPE_INVALID", "当前篇目不属于冻结抽样集合", 409)
        operation = payload["operation"]
        if operation in {"confirm", "fail"}:
            if len(items) != 1:
                raise AppError("WORK_SELECTION_INVALID", "请选择一篇进行检查", 422)
            if operation == "confirm":
                await service.proofreading.confirm(
                    set_id,
                    revisions[0],
                    payload["candidate_hash"],
                    actor,
                    {
                        k: payload.get(k, False)
                        for k in ("text_checked", "images_checked", "audio_listened")
                    },
                )
            else:
                await service.proofreading.fail(set_id, revisions[0], payload["error_code"], actor)
        elif operation == "append":
            await service.proofreading.append_samples(set_id, revisions, actor)
        elif operation == "bulk-confirm":
            await service.proofreading.bulk_confirm(
                set_id,
                actor,
                payload["collection_hash"],
                revisions,
                payload["accept_normal_results"],
                key,
            )
        elif operation == "complete":
            await service.proofreading.complete(
                set_id, actor, allow_abnormal_drafts=payload.get("allow_abnormal_drafts", False)
            )
        else:
            raise AppError("WORK_COMMAND_INVALID", "抽样操作无效", 422)
        result.proofreading_set_id = set_id
    elif action in {"publication-preview", "publication-confirm", "publication-retry"}:
        if service.publication is None:
            raise AppError("WORK_PUBLICATION_UNAVAILABLE", "发布服务未配置", 503)
        if action == "publication-preview":
            candidates = []
            for item in items:
                candidate = publication_candidate(item, order["content_type"])
                facts = await service.publication.inspect_candidate(candidate)
                if not facts.get("ready"):
                    raise AppError(
                        "PUBLICATION_CANDIDATE_NOT_READY", "当前候选尚未满足发布条件", 409
                    )
                candidate["warning_codes"] = facts.get("warning_codes", [])
                candidates.append(candidate)
            state = await service.publication.preview(
                candidates, actor, token=request["delegate_id"]
            )
            result.publication_token = state["token"]
            refs["publication_token"] = state["token"]
        else:
            token = payload["publication_token"]
            if token != order["publication_token"]:
                raise AppError("WORK_PUBLICATION_SCOPE_INVALID", "发布范围不属于此工作单", 409)
            state = await service.publication.recover(token, actor)
            frozen = {c["revision_id"] for c in state["candidates"]}
            revisions = request["selected_revision_ids"]
            if not set(revisions) <= frozen:
                raise AppError("WORK_PUBLICATION_SCOPE_INVALID", "篇目超出冻结发布范围", 409)
            if not revisions:
                raise AppError("WORK_SELECTION_EMPTY", "冻结范围没有所选失败项", 409)
            state = await service.publication.confirm(
                token,
                actor,
                key,
                manual_confirmed=payload["manual_confirmed"],
                expected_version=payload["range_version"],
                collection_hash=payload["collection_hash"],
                selected_revision_ids=revisions,
            )
            result.publication_token, result.stage_id, result.status = (
                token,
                state["stage_id"],
                "QUEUED",
            )
    elif action == "stage-retry":
        if service.stage_admin is None:
            raise AppError("WORK_STAGE_RETRY_UNAVAILABLE", "阶段恢复服务未配置", 503)
        if not any(
            t["stage_id"] == payload["stage_id"] and t["current"] for i in items for t in i["tasks"]
        ):
            raise AppError("WORK_STAGE_SCOPE_INVALID", "阶段不属于所选当前篇目", 409)
        delegated = await service.stage_admin.retry(
            payload["stage_id"],
            actor,
            key,
            {
                k: payload[k]
                for k in (
                    "expected_fence",
                    "input_hash",
                    "config_version",
                    "expected_edit_version",
                    "manual_confirmed",
                    "acknowledged_charged_retry",
                )
            },
        )
        result.stage_id, result.status = delegated["stage_id"], "QUEUED"
    elif action == "organize":
        stages = []
        async with service.sessions() as session, session.begin():
            for item in items:
                if not item["current"] or order["content_type"] != "dialogue":
                    continue
                current = await service.proofreading.current(
                    session, candidate_scope([item], order["content_type"])[0]
                )
                stage = await schedule_audio_within(
                    session,
                    service.sessions,
                    item["revision_id"],
                    SceneContent.model_validate(current),
                )
                if stage:
                    stages.append(stage)
        if not stages:
            raise AppError("WORK_ORGANIZE_NO_AUDIO", "所选篇目没有可继续的场景音频任务", 409)
        result.stage_id, result.status = stages[0], "QUEUED"
    elif action == "cancel":
        async with service.sessions() as session, session.begin():
            if not order["import_batch_id"]:
                raise AppError("WORK_ORDER_NOT_CANCELLABLE", "手工草稿不能通过取消删除", 409)
            await service.imports.batch(
                session, order["import_batch_id"], True, preview_hash_only=True
            )
            cancellable = []
            for item in items:
                if item["content_id"]:
                    continue
                import_row = (
                    await session.execute(
                        text(
                            "SELECT i.status,w.cancelled,i.content_id FROM content_import_item i "
                            "JOIN content_work_item w ON w.import_item_id=i.public_id "
                            "WHERE i.public_id=:id AND w.public_id=:item FOR UPDATE"
                        ),
                        {"id": item["import_item_id"], "item": item["item_id"]},
                    )
                ).first()
                if (
                    import_row
                    and import_row.status == "CANCELLED"
                    and import_row.cancelled
                    and not import_row.content_id
                ):
                    # The exact frozen cancellation may have committed before its outer result.
                    cancellable.append(item["item_id"])
                    continue
                if import_row is None or import_row.status != "PENDING":
                    continue
                started = await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM content_import_stage WHERE item_id=:id AND "
                        "(attempt>0 OR (stage='CONFIRM' AND status IN "
                        "('PENDING','QUEUED','RUNNING','RETRY')))"
                    ),
                    {"id": order["import_batch_id"]},
                )
                # Finished validation is expected; active confirmation prevents cancellation.
                if started:
                    running = await session.scalar(
                        text(
                            "SELECT COUNT(*) FROM content_import_stage WHERE item_id=:id AND "
                            "stage='CONFIRM' AND status IN ('PENDING','QUEUED','RUNNING','RETRY')"
                        ),
                        {"id": order["import_batch_id"]},
                    )
                    if running:
                        continue
                await session.execute(
                    text(
                        "UPDATE content_work_item SET cancelled=1 WHERE public_id=:id AND "
                        "content_id IS NULL"
                    ),
                    {"id": item["item_id"]},
                )
                await session.execute(
                    text(
                        "UPDATE content_import_item SET status='CANCELLED' "
                        "WHERE public_id=:id AND status='PENDING'"
                    ),
                    {"id": item["import_item_id"]},
                )
                cancellable.append(item["item_id"])
            if not cancellable:
                raise AppError("WORK_ORDER_NOT_CANCELLABLE", "没有尚未开始且未导入的篇目", 409)
            result.selected_item_ids = cancellable
            remaining = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM content_work_item WHERE work_order_id=:id AND cancelled=0"
                ),
                {"id": identity},
            )
            if not remaining:
                refs["status"] = "CANCELLED"
        if refs.get("status") == "CANCELLED":
            # The existing batch cancellation blocks legacy confirmation and preserves successes.
            await service.imports.cancel(order["import_batch_id"], actor)
    return result, refs


async def freeze_publication_request(
    service: "WorkOrderService",
    session: AsyncSession,
    action: str,
    request: dict[str, Any],
    actor: str,
    command_id: str,
) -> None:
    if service.publication is None:
        raise AppError("WORK_PUBLICATION_UNAVAILABLE", "发布服务未配置", 503)
    payload, order = request["payload"], request["order"]
    token = payload["publication_token"]
    if token != order["publication_token"]:
        raise AppError("WORK_PUBLICATION_SCOPE_INVALID", "发布范围不属于此工作单", 409)
    state = await service.publication._row(session, token, actor)
    membership = {i["revision_id"] for i in request["items"]}
    frozen = {c["revision_id"] for c in state["candidates"]}
    key = snapshot_hash(["work-command", command_id])
    replay = state["results"]["commands"].get(publication_digest(key))
    if replay is not None:
        revisions = replay["selected_revision_ids"]
    elif action == "publication-retry":
        revisions = [
            r
            for r in sorted(membership & frozen)
            if state["results"]["items"].get(r, {}).get("status") in {"FAILED", "UNKNOWN"}
        ]
    else:
        revisions = sorted(membership)
    if not revisions:
        raise AppError("WORK_SELECTION_EMPTY", "冻结范围没有所选失败项", 409)
    if not set(revisions) <= frozen or not set(revisions) <= membership:
        raise AppError("WORK_PUBLICATION_SCOPE_INVALID", "篇目超出冻结发布范围", 409)
    request["selected_revision_ids"] = revisions
    request["items"] = [i for i in request["items"] if i["revision_id"] in revisions]


def candidate_scope(items: list[dict[str, Any]], content_type: str) -> list[dict[str, Any]]:
    # Duplicate stable content can occur in legacy imports; freeze a unique actual revision.
    result = {}
    for item in items:
        if not item["revision_id"] or not item["candidate_hash"]:
            continue
        result[item["revision_id"]] = {
            "content_type": content_type,
            "content_id": item["content_id"],
            "revision_id": item["revision_id"],
            "candidate_hash": item["candidate_hash"],
        }
    return list(result.values())


def publication_candidate(item: dict[str, Any], content_type: str) -> dict[str, Any]:
    result = {
        "content_type": content_type,
        "content_id": item["content_id"],
        "revision_id": item["revision_id"],
        "candidate_hash": item["candidate_hash"],
        "expected_version": item["content_version"],
        "warning_codes": [],
    }
    if content_type == "short_reading":
        result["expected_edit_version"] = item["edit_version"]
    return result
