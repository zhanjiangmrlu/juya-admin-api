"""Actual import failure scope and durable accumulated results."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tests.integration.test_content_work_order_recovery_sql import imported_order
from tests.integration.test_content_work_orders import db as work_order_db
from tests.integration.test_content_work_orders import manual

db = work_order_db


@pytest.mark.asyncio
async def test_retry_returns_retained_success_and_partial_stage_is_not_success(db):
    order, preview = await imported_order(db, 2)
    first, second = [item.import_item_id for item in preview.items]
    success = await db.imports.confirm(
        preview.batch_id,
        preview.preview_hash,
        preview.preview_version,
        [first],
        ["default_cover"],
        [],
        db.actor,
        "first",
    )
    failed = await db.imports.confirm(
        preview.batch_id,
        preview.preview_hash,
        preview.preview_version,
        [second],
        [],
        [],
        db.actor,
        "failed",
    )
    assert failed["status"] == "PARTIAL"
    async with db.sessions() as session, session.begin():
        stage = await db.imports.stages.ensure_within(
            session, preview.batch_id, "CONFIRM", "a" * 64, "retry-test"
        )
    claim = await db.imports.stages.claim(stage, "test", datetime.now(UTC))
    assert claim
    await db.imports.stages.finish_import(claim, failed, datetime.now(UTC))
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT status FROM content_import_stage WHERE public_id=:id"), {"id": stage}
            )
            == "FAILED"
        )
    retried = await db.imports.confirm(
        preview.batch_id,
        preview.preview_hash,
        preview.preview_version,
        [second],
        ["default_cover"],
        [],
        db.actor,
        "retry",
    )
    assert retried["status"] == "CONFIRMED"
    assert {item["item_id"] for item in retried["items"]} == {first, second}
    assert next(i for i in retried["items"] if i["item_id"] == first) == success["items"][0]
    assert (await db.service.items(order.work_order_id)).counts.failed == 0


@pytest.mark.asyncio
async def test_current_success_supersedes_historical_failed_stage(db):
    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    async with db.sessions() as session, session.begin():
        old = await db.imports.stages.ensure_within(
            session, item.revision_id, "ORGANIZE", "a" * 64, "v1"
        )
    first = await db.imports.stages.claim(old, "test", datetime.now(UTC))
    await db.imports.stages.fail(first, "OLD_FAILURE", datetime.now(UTC), False)
    async with db.sessions() as session, session.begin():
        new = await db.imports.stages.ensure_within(
            session, item.revision_id, "ORGANIZE", "b" * 64, "v1"
        )
    second = await db.imports.stages.claim(new, "test", datetime.now(UTC))
    await db.imports.stages.finish(second, {"prepared": True}, datetime.now(UTC))
    current = await db.service.item(order.work_order_id, item.item_id)
    assert current.production_status == "normal"
    assert len(current.tasks) == 2 and sum(t.current for t in current.tasks) == 1


@pytest.mark.asyncio
async def test_organize_recovers_committed_delegate_before_rechecking_changed_content(
    db, monkeypatch
):
    from juya_admin_api.modules.content_production import commands
    from tests.integration.test_content_work_order_recovery_sql import expire_command

    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    original = commands.delegate

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("lost outer response")

    monkeypatch.setattr(commands, "delegate", lost)
    payload = dict(expected_version=order.version, scope="all", selected_item_ids=[])
    with pytest.raises(RuntimeError):
        await db.service.command(order.work_order_id, "organize", payload, db.actor, "organize")
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE short_reading_revision SET content_snapshot="
                "JSON_SET(content_snapshot,'$.title_en','Edited after preparation'),"
                "edit_version=edit_version+1 WHERE public_id=:id"
            ),
            {"id": item.revision_id},
        )
    await expire_command(db, order.work_order_id)
    monkeypatch.setattr(commands, "delegate", original)
    recovered = await db.service.command(
        order.work_order_id, "organize", payload, db.actor, "organize"
    )
    assert recovered.status == "SUCCEEDED"
