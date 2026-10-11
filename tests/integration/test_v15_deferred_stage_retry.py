"""Deferred CONFIRM/MATERIAL_BIND recovery receipts survive subsequent cancellation."""

import asyncio
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
import test_v15_worker_dispatch as dispatch
from PIL import Image
from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content_import.admin_stages import StageAdminStore
from juya_admin_api.shared.errors import AppError

dispatched_runtime = dispatch.dispatched_runtime


async def failed_confirmation(db):
    batch_id, stage = await dispatch.queued_document(db)
    await db.services.imports.validate(batch_id, stage)
    preview = await db.services.imports.preview(batch_id)
    result = await db.services.imports.queue_confirmation(
        batch_id,
        {
            "preview_hash": preview["preview_hash"],
            "preview_version": preview["preview_version"],
            "selected": [preview["items"][0]["item_id"]],
            "acknowledged_defaults": ["default_cover"],
            "confirmed_conflicts": [],
        },
        db.actor,
        uuid4().hex,
    )
    return batch_id, result["stage_id"]


async def failed_material_binding(db):
    created = await db.services.readings.command(
        "create",
        "new",
        db.actor,
        uuid4().hex,
        {"series_id": db.series, "content": {"title_en": "Bank", "title_zh": "银行"}},
    )
    image = BytesIO()
    Image.new("RGB", (32, 32), (12, 34, 56)).save(image, format="PNG")
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("images/Bank.png", image.getvalue())
    batch = await db.services.materials.create(
        db.series, "short_reading", package.getvalue(), db.actor, uuid4().hex
    )
    await db.services.materials.run(batch["stage_id"], "material-validator", datetime.now(UTC))
    result = await db.services.materials.queue_bind(
        batch["batch_id"],
        created["revision_id"],
        1,
        True,
        [{"path": "images/Bank.png", "role": "COVER_IMAGE"}],
        db.actor,
        uuid4().hex,
    )
    return batch["batch_id"], result["stage_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["CONFIRM", "MATERIAL_BIND"])
async def test_deferred_retry_receipt_first_after_cancel_and_old_fence(dispatched_runtime, kind):
    db = dispatched_runtime
    batch_id, stage_id = await (
        failed_confirmation(db) if kind == "CONFIRM" else failed_material_binding(db)
    )
    stages = db.services.imports.stages
    old = await stages.claim(stage_id, "failed-deferred-worker", datetime.now(UTC))
    assert old
    await stages.fail(old, "FIXTURE_INTERRUPTED", datetime.now(UTC), retry=False)
    admin = StageAdminStore(
        db.services.imports, db.services.authoring, db.services.materials, db.services.candidates
    )
    before = await admin.get(stage_id)
    request = {
        "manual_confirmed": True,
        "expected_fence": before["fencing_version"],
        "input_hash": before["input_hash"],
        "config_version": before["config_version"],
    }
    async with db.sessions() as session:
        if kind == "CONFIRM":
            key = await session.scalar(
                text(
                    "SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.key')) FROM admin_outbox "
                    "WHERE event_type='CONTENT_IMPORT_CONFIRM' AND "
                    "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.stage_id'))=:stage"
                ),
                {"stage": stage_id},
            )
        else:
            batch = await db.services.imports.batch(session, batch_id)
            key = decode(batch["manifest"])["binding_requests"][stage_id]["key"]
    assert isinstance(key, str) and key
    # Reusing the caller's original enqueue key is valid in the distinct retry endpoint.
    # Its replacement execution must not replay that endpoint's original failed stage.
    concurrent = await asyncio.gather(
        *[admin.retry(stage_id, db.actor, key, dict(request)) for _ in range(3)]
    )
    result = concurrent[0]
    assert concurrent == [result] * 3
    new_stage = result["stage_id"]
    assert new_stage != stage_id
    old_state = await admin.get(stage_id)
    assert old_state["status"] == "CANCELLED"
    assert old_state["fencing_version"] > before["fencing_version"]
    new_state = await admin.get(new_stage)
    async with db.sessions() as session:
        executions = await session.scalar(
            text("SELECT COUNT(*) FROM content_import_stage WHERE item_id=:batch AND stage=:stage"),
            {"batch": batch_id, "stage": before["stage"]},
        )
        queued_events = await session.scalar(
            text(
                "SELECT COUNT(*) FROM admin_outbox WHERE status='PENDING' AND "
                "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.stage_id'))=:stage"
            ),
            {"stage": new_stage},
        )
    assert executions == 2  # Original failed execution and exactly one replacement.
    assert queued_events == 1
    if kind == "CONFIRM":
        # CONFIRM's existing public enqueue contract persists PENDING plus its durable outbox.
        # The real scheduler then acquires the QUEUED lease before delivering to the worker.
        assert new_state["status"] == "PENDING"
        assert await stages.enqueue(new_stage, datetime.now(UTC))
        new_state = await admin.get(new_stage)
    assert new_state["status"] == "QUEUED" and new_state["lease_until"]
    with pytest.raises(AppError) as lost:
        await stages.finish(old, {"obsolete": True}, datetime.now(UTC))
    assert lost.value.code == "IMPORT_STAGE_LEASE_LOST"
    assert await stages.claim(stage_id, "obsolete-redelivery", datetime.now(UTC)) is None
    await db.services.imports.cancel(batch_id, db.actor)
    assert await admin.retry(stage_id, db.actor, key, request) == result
    with pytest.raises(AppError) as reused:
        await admin.retry(
            stage_id, db.actor, key, request | {"expected_fence": request["expected_fence"] + 1}
        )
    assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"
    with pytest.raises(AppError) as cancelled:
        await admin.retry(stage_id, db.actor, uuid4().hex, request)
    assert cancelled.value.code == "IMPORT_CANCELLED"
    async with db.sessions() as session:
        receipts = await session.scalar(
            text(
                "SELECT COUNT(*) FROM idempotency_record "
                "WHERE scope=:scope AND actor_id=:actor AND idempotency_key=:key"
            ),
            {"scope": "v15.stage.retry:" + stage_id, "actor": db.actor, "key": key},
        )
    assert receipts == 1
    assert (await admin.get(new_stage))["status"] == "CANCELLED"
