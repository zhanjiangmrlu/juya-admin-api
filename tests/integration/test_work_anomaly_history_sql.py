"""Exact work-order anomaly history on the ordinary private SQL runner."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_production_final_fixes_sql import command, members
from tests.integration.test_content_work_orders import db as work_db
from tests.integration.test_content_work_orders import manual, reading

db = work_db


async def fail_a(db, order, row):
    state = await db.service.sampling(order.work_order_id)
    await command(
        db,
        order,
        "sample-action",
        scope="selected",
        selected_item_ids=[row.item_id],
        observed_item_id=row.item_id,
        operation="fail",
        candidate_hash=row.candidate_hash,
        collection_hash=state.collection_hash,
        error_code="MANUAL_REVIEW_REQUIRED",
    )


async def subset_then_all(db, order, rows):
    await command(db, order, "sampling", scope="selected", selected_item_ids=[rows[1].item_id])
    await command(db, order, "sampling", scope="all")
    return await db.service.sampling(order.work_order_id)


async def inspect_one(db, order, row):
    await command(
        db,
        order,
        "sample-action",
        scope="selected",
        selected_item_ids=[row.item_id],
        operation="confirm",
        candidate_hash=row.candidate_hash,
        text_checked=True,
    )


async def publish(db, order, row):
    current = await db.service.item(order.work_order_id, row.item_id)
    await db.readings.command(
        "publish",
        current.content_id,
        db.actor,
        uuid4().hex,
        dict(
            expected_version=current.content_version,
            revision_id=current.revision_id,
            manual_confirmed=True,
        ),
    )
    assert (await db.service.item(order.work_order_id, row.item_id)).published


@pytest.mark.asyncio
@pytest.mark.parametrize("old_audit", [False, True])
async def test_unchanged_failure_survives_intermediate_subset_sampling(db, monkeypatch, old_audit):
    # Reviewer counterexample: a valid server draw selects B, never known-bad A.
    monkeypatch.setattr(
        "secrets.SystemRandom.sample", lambda self, population, count: population[-count:]
    )
    order, rows = await members(db, count=2)
    initial = await command(db, order, "sampling", scope="all")
    await fail_a(db, order, rows[0])
    if old_audit:
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE audit_event SET after_summary=JSON_REMOVE(after_summary,"
                    "'$.candidates','$.reported_revision') WHERE action='proofreading.fail' "
                    "AND object_public_id=:id"
                ),
                {"id": initial.proofreading_set_id},
            )
    frozen = (await db.proof.get(initial.proofreading_set_id))["candidates"]
    await command(db, order, "sampling", scope="selected", selected_item_ids=[rows[1].item_id])
    omitted = await db.service.item(order.work_order_id, rows[0].item_id)
    assert omitted.production_status == "review_required" and not omitted.publishable
    await command(db, order, "sampling", scope="all")
    latest = await db.service.sampling(order.work_order_id)
    unchanged = next(c for c in latest.candidates if c.revision_id == rows[0].revision_id)
    assert unchanged.candidate_hash == rows[0].candidate_hash
    assert not unchanged.normal and unchanged.failure["error_code"] == "MANUAL_REVIEW_REQUIRED"
    assert latest.sample_ids == [rows[1].revision_id]
    await inspect_one(db, order, rows[1])
    latest = await db.service.sampling(order.work_order_id)
    await command(
        db,
        order,
        "sample-action",
        scope="normal",
        operation="bulk-confirm",
        collection_hash=latest.collection_hash,
        accept_normal_results=True,
    )
    with pytest.raises(AppError) as incomplete:
        await command(db, order, "sample-action", operation="complete")
    assert incomplete.value.code == "PROOFREADING_INCOMPLETE"
    await command(db, order, "sample-action", operation="complete", allow_abnormal_drafts=True)
    with pytest.raises(AppError) as denied:
        await publish(db, order, rows[0])
    assert denied.value.code == "PUBLISH_CHECK_FAILED"
    assert denied.value.details == {"error_codes": ["PROOFREADING_REQUIRED"]}
    assert not (await db.service.item(order.work_order_id, rows[0].item_id)).published
    await publish(db, order, rows[1])
    assert (await db.proof.get(initial.proofreading_set_id))["candidates"] == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution,old_audit",
    [
        ("individual", False),
        ("individual", True),
        ("correct_hash", False),
        ("individual_then_fail", False),
    ],
)
async def test_lawful_resolution_and_later_failure_order(db, monkeypatch, resolution, old_audit):
    monkeypatch.setattr(
        "secrets.SystemRandom.sample", lambda self, population, count: population[-count:]
    )
    order, rows = await members(db, count=2)
    await command(db, order, "sampling", scope="all")
    await fail_a(db, order, rows[0])
    # Resolve after the anomaly's original set was already replaced.
    await subset_then_all(db, order, rows)
    if resolution == "correct_hash":
        current = await db.service.item(order.work_order_id, rows[0].item_id)
        await db.service.correct(
            order.work_order_id,
            current.item_id,
            dict(
                expected_version=(await db.service.get(order.work_order_id)).version,
                expected_revision_id=current.revision_id,
                expected_edit_version=current.edit_version,
                expected_content_version=current.content_version,
                candidate_hash=current.candidate_hash,
                content=reading("Actual corrected content"),
            ),
            db.actor,
            uuid4().hex,
        )
    else:
        await inspect_one(db, order, rows[0])
    if old_audit:
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE audit_event SET after_summary=JSON_REMOVE(after_summary,"
                    "'$.candidates','$.reported_revision') WHERE object_type='proofreading_set' "
                    "AND action IN ('proofreading.fail','proofreading.confirm')"
                )
            )
    latest = await subset_then_all(db, order, rows)
    a = next(c for c in latest.candidates if c.revision_id == rows[0].revision_id)
    assert a.normal and a.failure is None
    current = await db.service.item(order.work_order_id, rows[0].item_id)
    assert (
        current.candidate_hash == rows[0].candidate_hash
        if resolution != "correct_hash"
        else current.candidate_hash != rows[0].candidate_hash
    )
    if resolution == "individual_then_fail":
        await fail_a(db, order, current)
        latest = await subset_then_all(db, order, rows)
        a = next(c for c in latest.candidates if c.revision_id == rows[0].revision_id)
        assert not a.normal and a.failure
        with pytest.raises(AppError) as denied:
            await publish(db, order, rows[0])
        assert denied.value.code == "PUBLISH_CHECK_FAILED"
        assert "PROOFREADING_REQUIRED" in denied.value.details["error_codes"]
        return
    # Only B is inspected in the newest set: resolved A is legitimately bulk-approved.
    await inspect_one(db, order, rows[1])
    latest = await db.service.sampling(order.work_order_id)
    await command(
        db,
        order,
        "sample-action",
        scope="normal",
        operation="bulk-confirm",
        collection_hash=latest.collection_hash,
        accept_normal_results=True,
    )
    await command(db, order, "sample-action", operation="complete")
    await publish(db, order, rows[0])


@pytest.mark.asyncio
async def test_foreign_order_and_independent_legacy_sets_do_not_supply_anomaly_history(db):
    order, rows = await members(db, count=2)
    await command(db, order, "sampling", scope="all")
    other = await manual(db, key=uuid4().hex, content=reading("Other source"))
    async with db.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE content_work_item SET content_id=:content WHERE work_order_id=:order"),
            {"content": rows[0].content_id, "order": other.work_order_id},
        )
    foreign = (await db.service.items(other.work_order_id)).items[0]
    await command(db, other, "sampling", scope="all")
    await fail_a(db, other, foreign)
    legacy = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=rows[0].content_id,
                revision_id=rows[0].revision_id,
                candidate_hash=rows[0].candidate_hash,
            )
        ],
        db.actor,
    )
    await db.proof.fail(
        legacy["public_id"], rows[0].revision_id, "MANUAL_REVIEW_REQUIRED", db.actor
    )
    latest = await subset_then_all(db, order, rows)
    assert all(c.normal and c.failure is None for c in latest.candidates)
    assert (await db.proof.get(legacy["public_id"]))["status"] == "FAILED"
    assert (
        await db.service.item(other.work_order_id, foreign.item_id)
    ).production_status == "review_required"
