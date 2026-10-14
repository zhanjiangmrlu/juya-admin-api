"""Work-order acceptance uses only the runner-owned migrated MySQL."""

import asyncio
import importlib.util
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.production_store import ProductionStore, encode
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.shared.errors import AppError


@pytest_asyncio.fixture
async def db(tmp_path):
    assert importlib.util.find_spec("juya_admin_api.modules.content_production"), (
        "persistent work-order domain is missing"
    )
    from juya_admin_api.modules.content_production.service import WorkOrderService

    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("runner-owned migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    production = ProductionStore(sessions)
    proof = ProofreadingStore(sessions)
    readings = ShortReadingStore(sessions, check_confirmation=proof.is_confirmed_within)
    series = await production.create_series("Work orders", "wo-" + uuid4().hex, None)
    actor = uuid4().hex[:26]

    async def bind(*args):
        raise AssertionError("no paid or media operation on work-order reads")

    imports = ImportService(sessions, tmp_path, production, readings, bind)
    service = WorkOrderService(sessions, imports, production, readings, proof)
    try:
        yield SimpleNamespace(
            sessions=sessions,
            production=production,
            readings=readings,
            proof=proof,
            imports=imports,
            service=service,
            series=series["id"],
            actor=actor,
        )
    finally:
        await engine.dispose()


def reading(title="Original"):
    return {
        "title_en": title,
        "title_zh": "原文",
        "category": "生活",
        "topic": "交流",
        "level": "A1",
        "safe_intro": "介绍",
        "paragraphs_en": ["Hello."],
        "translation_zh": "你好。",
        "copyright_status": "CONFIRMED",
        "copyright_source": "Owned",
    }


async def manual(db, key="manual", content=None):
    return await db.service.manual(
        series_id=db.series,
        content_type="short_reading",
        content_id=None,
        content=content or reading(),
        actor=db.actor,
        key=key,
    )


@pytest.mark.asyncio
async def test_manual_reload_idempotency_and_immutable_missing_original(db):
    order = await manual(db)
    replay = await manual(db)
    assert replay.work_order_id == order.work_order_id
    page = await db.service.items(order.work_order_id)
    assert page.counts.total == 1 and page.counts.normal == 1
    detail = await db.service.item(order.work_order_id, page.items[0].item_id)
    assert detail.current.template == "short_reading"
    assert detail.current.content.title_en == "Original"
    assert detail.source_available is False and detail.source_snapshot is None
    restored = await db.service.get(order.work_order_id)
    assert restored.counts == page.counts
    with pytest.raises(AppError) as conflict:
        await manual(db, content=reading("Different"))
    assert conflict.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.asyncio
async def test_correction_is_atomic_versioned_and_preserves_original(db):
    order = await manual(db)
    before_page = await db.service.items(order.work_order_id)
    item = before_page.items[0]
    original = {"schema_version": 1, "blocks": [{"text": "Original"}]}
    async with db.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE content_work_item SET source_snapshot=:s WHERE public_id=:id"),
            {"s": encode(original), "id": item.item_id},
        )
    payload = {
        "expected_version": order.version,
        "expected_revision_id": item.revision_id,
        "expected_edit_version": item.edit_version,
        "expected_content_version": item.content_version,
        "candidate_hash": item.candidate_hash,
        "content": reading("Corrected"),
        "resolved_issue_ids": [],
    }
    result = await db.service.correct(order.work_order_id, item.item_id, payload, db.actor, "save")
    again = await db.service.correct(order.work_order_id, item.item_id, payload, db.actor, "save")
    assert result == again
    detail = await db.service.item(order.work_order_id, item.item_id)
    assert detail.current.content.title_en == "Corrected"
    assert detail.source_snapshot == original
    assert detail.edit_version == item.edit_version + 1
    assert (await db.service.get(order.work_order_id)).version == order.version
    after_page = await db.service.items(order.work_order_id)
    assert getattr(before_page, "collection_hash", None)
    assert before_page.collection_hash != after_page.collection_hash
    assert before_page.version == after_page.version
    with pytest.raises(AppError) as error:
        await db.service.correct(order.work_order_id, item.item_id, payload, db.actor, "stale")
    assert error.value.code in {"CONTENT_VERSION_CONFLICT", "WORK_ITEM_VERSION_CONFLICT"}


@pytest.mark.asyncio
async def test_membership_and_sampling_are_order_scoped(db):
    one, two = await manual(db, "one"), await manual(db, "two")
    item1 = (await db.service.items(one.work_order_id)).items[0]
    item2 = (await db.service.items(two.work_order_id)).items[0]
    with pytest.raises(AppError) as error:
        await db.service.item(one.work_order_id, item2.item_id)
    assert error.value.code == "WORK_ITEM_NOT_FOUND"
    cohort = await db.service.command(
        one.work_order_id,
        "sampling",
        {
            "expected_version": one.version,
            "scope": "normal",
            "selected_item_ids": [],
        },
        db.actor,
        "sample",
    )
    assert cohort.proofreading_set_id
    proof = await db.proof.get(cohort.proofreading_set_id)
    assert [row["revision_id"] for row in proof["candidates"]] == [item1.revision_id]
    assert proof["sample_ids"] == [item1.revision_id]
    replay = await db.service.command(
        one.work_order_id,
        "sampling",
        {
            "expected_version": one.version,
            "scope": "normal",
            "selected_item_ids": [],
        },
        db.actor,
        "sample",
    )
    assert replay == cohort
    with pytest.raises(AppError) as error:
        await db.service.command(
            one.work_order_id,
            "sampling",
            {
                "expected_version": one.version,
                "scope": "all",
                "selected_item_ids": [],
            },
            db.actor,
            "new",
        )
    assert error.value.code == "WORK_ORDER_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_global_thousand_counts_filter_before_paging(db):
    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO content_work_item(public_id,work_order_id,position,content_id) "
                "VALUES(:id,:order,:pos,:content)"
            ),
            [
                {
                    "id": uuid4().hex,
                    "order": order.work_order_id,
                    "pos": n,
                    "content": item.content_id,
                }
                for n in range(2, 1001)
            ],
        )
    page = await db.service.items(order.work_order_id, page=2, page_size=30, status="normal")
    assert page.total == 1000 and page.counts.total == 1000 and page.counts.normal == 1000
    assert len(page.items) == 30
    assert (await db.service.items(order.work_order_id, status="failed")).total == 0


@pytest.mark.asyncio
async def test_revision_and_slice_tasks_follow_authoritative_outbox(db):
    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    async with db.sessions() as session, session.begin():
        stage = await db.imports.stages.ensure_within(
            session, "opaque-sentence-hash", "SENTENCE_SLICE", snapshot_hash("slice"), "v1"
        )
        await session.execute(
            text(
                "INSERT INTO admin_outbox(event_id,event_type,aggregate_public_id,payload) "
                "VALUES(:id,'CONTENT_SENTENCE_SLICE',:r,:p)"
            ),
            {
                "id": uuid4().hex[:26],
                "r": item.revision_id,
                "p": encode(
                    {"stage_id": stage, "revision_id": item.revision_id, "sentence_id": "s1"}
                ),
            },
        )
    task = (await db.service.item(order.work_order_id, item.item_id)).tasks[0]
    assert task.stage_id == stage and task.revision_id == item.revision_id
    assert task.sentence_id == "s1"
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM content_work_item_stage WHERE stage_id=:id"),
                {"id": stage},
            )
            == 1
        )
        assert await session.scalar(text("SELECT version FROM schema_version WHERE id=1")) >= 22


@pytest.mark.asyncio
async def test_from_batch_original_and_partial_import_recovery(db):
    from tests.integration.test_v15_import_flow import import_zip

    created = await db.imports.create(
        series_id=db.series,
        content_type="dialogue",
        mode="ZIP",
        data=import_zip(uuid4().hex, bad=True),
        actor=db.actor,
        key="upload",
    )
    async with db.sessions() as session:
        stage = await session.scalar(
            text("SELECT public_id FROM content_import_stage WHERE item_id=:id"),
            {"id": created["batch_id"]},
        )
    await db.imports.validate(created["batch_id"], stage)
    order = await db.service.from_batch(created["batch_id"], db.actor, "register")
    assert (
        await db.service.from_batch(created["batch_id"], db.actor, "again")
    ).work_order_id == order.work_order_id
    page = await db.service.items(order.work_order_id)
    assert page.counts.total == 2 and page.counts.review_required == 2
    first = await db.service.item(order.work_order_id, page.items[0].item_id)
    assert first.source_available and first.source_snapshot["title"]["title_en"] == "Hello"
    preview = await db.service.import_preview(order.work_order_id)
    await db.imports.confirm(
        created["batch_id"],
        preview.preview_hash,
        preview.preview_version,
        [i.import_item_id for i in page.items],
        ["default_cover"],
        [],
        db.actor,
        "partial",
    )
    reloaded = await db.service.items(order.work_order_id)
    assert reloaded.items[0].content_id and reloaded.items[1].production_status == "failed"
    assert getattr(reloaded, "version", None) == order.version
    assert callable(getattr(db.service, "phases", None)), "read-only batch phase projection missing"
    phases = await db.service.phases(order.work_order_id)
    parsed = next(p for p in phases.phases if p.key == "parsing")
    # Legacy scene fixture lacks phase preparation evidence; VALIDATE alone cannot pass pairing
    assert parsed.counts.total == 2 and parsed.counts.review_required == 2
    assert all(sum(p.counts.model_dump(exclude={"total"}).values()) == 2 for p in phases.phases)
    receipt = await db.service.command(
        order.work_order_id,
        "import-retry",
        {
            "expected_version": order.version,
            "preview_hash": preview.preview_hash,
            "preview_version": preview.preview_version,
            "acknowledged_defaults": ["default_cover"],
            "confirmed_conflicts": [],
            "scope": "failed",
            "selected_item_ids": [],
        },
        db.actor,
        "retry",
    )
    async with db.sessions() as session:
        outbox = await session.scalar(
            text(
                "SELECT payload FROM admin_outbox WHERE JSON_UNQUOTE(JSON_EXTRACT(payload,'$.stag"
                "e_id'))=:id AND event_type='CONTENT_IMPORT_CONFIRM'"
            ),
            {"id": receipt.stage_id},
        )
    from juya_admin_api.modules.content.production_store import decode

    assert decode(outbox)["confirmation"]["selected"] == [reloaded.items[1].import_item_id]
    # Reloading preview deliberately changes its frozen hash; inspect result after old-hash retry
    persisted = await db.service.import_preview(order.work_order_id)
    assert persisted.items[0].result is not None
    assert persisted.items[0].result.operation == "CREATE"
    assert persisted.items[0].result.status == "SUCCESS"
    assert persisted.items[1].result.status == "FAILED"
    assert persisted.items[1].result.error_code


@pytest.mark.asyncio
async def test_prepared_reading_scope_has_two_known_successful_text_checks_without_scene_audio(db):
    one = await manual(db, "prepared-one")
    two = await manual(db, "prepared-two", reading("Second"))
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE content_work_item SET work_order_id=:one,position=2 "
                "WHERE work_order_id=:two"
            ),
            {"one": one.work_order_id, "two": two.work_order_id},
        )
    state = await db.service.phases(one.work_order_id)
    parsed = next(p for p in state.phases if p.key == "parsing")
    assert parsed.counts.total == 2 and parsed.counts.succeeded == 2
    audio = next(p for p in state.phases if p.key == "audio")
    assert audio.counts.not_applicable == 2
    assert all(sum(p.counts.model_dump(exclude={"total"}).values()) == 2 for p in state.phases)


@pytest.mark.asyncio
async def test_actual_usage_does_not_use_legacy_default_region_as_provider_evidence(db):
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from juya_admin_api.modules.cloud_usage.models import attempts

    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    stage = uuid4().hex[:26]
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO content_import_stage(public_id,item_id,stage,input_hash,"
                "config_version,status) "
                "VALUES(:id,:revision,'AI_LEXICON',:hash,'v1','SUCCEEDED')"
            ),
            {"id": stage, "revision": item.revision_id, "hash": "a" * 64},
        )
        for number, metadata in enumerate(
            (
                {"model": "qwen-flash", "prompt_tokens": 128000, "completion_tokens": 1000},
                {
                    "model": "qwen-flash",
                    "provider_region": "cn-beijing",
                    "prompt_tokens": 128001,
                    "completion_tokens": 1000,
                },
            ),
            1,
        ):
            await session.execute(
                insert(attempts).values(
                    public_id=uuid4().hex[:26],
                    stage_id=stage,
                    attempt_number=number,
                    kind="AI_TOKENS",
                    period="2026-10-14",
                    reserved_units=200000,
                    actual_units=129000,
                    usage=metadata,
                    status="SUCCEEDED",
                    price_version="UNVERIFIED",
                    region="cn-beijing",
                    currency="CNY",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
    usage = await db.service.usage(order.work_order_id)
    assert len(usage.attempts) == 2 and usage.unpriced_attempt_count == 1
    by_number = {attempt.attempt_number: attempt for attempt in usage.attempts}
    assert by_number[1].list_price_estimate.amount_cny is None
    assert by_number[2].list_price_estimate.amount_cny == "0.0828006"
    assert usage.known_list_price_subtotal_cny == "0.0828006"
    assert getattr(usage, "source_text_utf8_bytes", None) == len("Hello.你好。".encode())
    assert usage.source_text_count == 1 and usage.source_unknown_count == 0


@pytest.mark.asyncio
async def test_cancel_preserves_existing_content(db):
    order = await manual(db)
    with pytest.raises(AppError) as error:
        await db.service.command(
            order.work_order_id,
            "cancel",
            {
                "expected_version": order.version,
                "scope": "all",
                "selected_item_ids": [],
            },
            db.actor,
            "cancel",
        )
    assert error.value.code == "WORK_ORDER_NOT_CANCELLABLE"


@pytest.mark.asyncio
async def test_uncertain_sampling_recovers_the_same_committed_set(db, monkeypatch):
    order = await manual(db)
    real_create = db.proof.create

    async def lost_result(*args, **kwargs):
        await real_create(*args, **kwargs)
        raise RuntimeError("transport result lost after commit")

    monkeypatch.setattr(db.proof, "create", lost_result)
    payload = {"expected_version": order.version, "scope": "normal", "selected_item_ids": []}
    with pytest.raises(RuntimeError):
        await db.service.command(order.work_order_id, "sampling", payload, db.actor, "lost")
    async with db.sessions() as session, session.begin():
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM proofreading_set WHERE created_by=:actor"),
                {"actor": db.actor},
            )
            == 1
        )
        await session.execute(
            text(
                "UPDATE content_work_command SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND "
                "WHERE work_order_id=:id"
            ),
            {"id": order.work_order_id},
        )
    monkeypatch.setattr(db.proof, "create", real_create)
    pending = (await db.service.get(order.work_order_id)).pending_commands
    assert len(pending) == 1 and pending[0].action == "sampling"
    recovered = await db.service.resume(order.work_order_id, pending[0].command_id, db.actor)
    assert recovered.proofreading_set_id
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM proofreading_set WHERE created_by=:actor"),
                {"actor": db.actor},
            )
            == 1
        )


@pytest.mark.asyncio
async def test_material_batch_uses_trusted_bound_revisions_not_import_rows(db):
    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    batch, stage = uuid4().hex[:26], uuid4().hex[:26]
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO content_import_batch(public_id,namespace,series_id,content_type,impo"
                "rt_mode,asset_id,input_hash,status,created_by,manifest) "
                "VALUES(:id,'v15-materials',:series,'short_reading','MATERIAL_ZIP',:asset,:hash,'"
                "BOUND',:actor,:manifest)"
            ),
            {
                "id": batch,
                "series": db.series,
                "asset": uuid4().hex,
                "hash": snapshot_hash("material"),
                "actor": db.actor,
                "manifest": encode(
                    {"binding_requests": {stage: {"request": {"revision_id": item.revision_id}}}}
                ),
            },
        )
        await session.execute(
            text(
                "INSERT INTO content_import_stage(public_id,item_id,stage,input_hash,config_versi"
                "on,status) VALUES(:id,:batch,'MATERIAL_BIND_one',:hash,'v1','SUCCEEDED')"
            ),
            {"id": stage, "batch": batch, "hash": snapshot_hash("material")},
        )
    attached = await db.service.from_batch(batch, db.actor, "material")
    page = await db.service.items(attached.work_order_id)
    assert page.counts.total == 1 and page.items[0].content_id == item.content_id
    detail = await db.service.item(attached.work_order_id, page.items[0].item_id)
    assert detail.source_available is False and detail.tasks[0].stage_id == stage
    with pytest.raises(AppError) as error:
        await db.service.import_preview(attached.work_order_id)
    assert error.value.code == "WORK_MATERIAL_USE_BINDINGS"


@pytest.mark.asyncio
async def test_changed_reading_revision_keeps_history_and_rejects_other_order_stale_edit(db):
    one = await manual(db)
    first = (await db.service.items(one.work_order_id)).items[0]
    two = await db.service.manual(
        series_id=db.series,
        content_type="short_reading",
        content_id=first.content_id,
        content=None,
        actor=db.actor,
        key="existing",
    )
    stale = (await db.service.items(two.work_order_id)).items[0]
    await db.readings.command(
        "revision",
        first.content_id,
        db.actor,
        "new-revision",
        {
            "source_revision_id": first.revision_id,
            "expected_version": first.content_version,
        },
    )
    current = await db.service.item(one.work_order_id, first.item_id)
    assert (
        current.revision_id != first.revision_id
        and current.original_revision_id == first.revision_id
    )
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM content_work_item_revision WHERE work_item_id=:id"),
                {"id": first.item_id},
            )
            == 2
        )
    with pytest.raises(AppError) as error:
        await db.service.correct(
            two.work_order_id,
            stale.item_id,
            {
                "expected_version": two.version,
                "expected_revision_id": stale.revision_id,
                "expected_edit_version": stale.edit_version,
                "expected_content_version": stale.content_version,
                "candidate_hash": stale.candidate_hash,
                "content": reading("Stale write"),
            },
            db.actor,
            "stale-other-order",
        )
    assert error.value.code == "WORK_ITEM_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_real_sampling_and_publication_are_subset_scoped_and_durable(db):
    from juya_admin_api.modules.content.publication_adapter import build_publication_service
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.service import ContentService

    db.service.publication = build_publication_service(
        db.sessions,
        db.imports.stages,
        db.production,
        ContentService(SQLAlchemyContentRepository(db.sessions)),
        db.readings,
    )
    order = await manual(db)
    good = (await db.service.items(order.work_order_id)).items[0]
    second = await db.readings.command(
        "create",
        "new",
        db.actor,
        "bad",
        {"series_id": db.series, "content": {"title_en": "Incomplete"}},
    )
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO content_work_item(public_id,work_order_id,position,content_id,origin"
                "al_revision_id) VALUES(:id,:order,2,:content,:revision)"
            ),
            {
                "id": uuid4().hex,
                "order": order.work_order_id,
                "content": second["content_id"],
                "revision": second["revision_id"],
            },
        )
    sample = await db.service.command(
        order.work_order_id,
        "sampling",
        {"expected_version": order.version, "scope": "normal", "selected_item_ids": []},
        db.actor,
        "sampling",
    )
    checked = await db.service.command(
        order.work_order_id,
        "sample-action",
        {
            "expected_version": sample.version,
            "scope": "selected",
            "selected_item_ids": [good.item_id],
            "operation": "confirm",
            "candidate_hash": good.candidate_hash,
            "text_checked": True,
        },
        db.actor,
        "check",
    )
    assert (await db.service.items(order.work_order_id)).counts.confirmed == 1
    completed = await db.service.command(
        order.work_order_id,
        "sample-action",
        {
            "expected_version": checked.version,
            "operation": "complete",
            "scope": "normal",
            "selected_item_ids": [],
        },
        db.actor,
        "complete",
    )
    preview = await db.service.command(
        order.work_order_id,
        "publication-preview",
        {
            "expected_version": completed.version,
            "scope": "all",
            "selected_item_ids": [],
        },
        db.actor,
        "publish-preview",
    )
    state = await db.service.publication_state(order.work_order_id, db.actor)
    assert [c.revision_id for c in state.candidates] == [good.revision_id]
    confirmed = await db.service.command(
        order.work_order_id,
        "publication-confirm",
        {
            "expected_version": preview.version,
            "scope": "publishable",
            "selected_item_ids": [],
            "publication_token": state.token,
            "range_version": state.version,
            "collection_hash": state.collection_hash,
            "manual_confirmed": True,
        },
        db.actor,
        "publish-confirm",
    )
    await db.service.publication.run(confirmed.stage_id, "work-order-test")
    final = await db.service.publication_state(order.work_order_id, db.actor)
    assert final.results[0].status == "SUCCEEDED"
    page = await db.service.items(order.work_order_id)
    assert page.counts.published == 1 and page.counts.review_required == 1
    assert page.items[0].production_status == "published" and page.items[0].published
    phases = await db.service.phases(order.work_order_id)
    final_phase = next(p for p in phases.phases if p.key == "publication")
    assert final_phase.counts.succeeded == final_phase.counts.review_required == 1


@pytest.mark.asyncio
async def test_partial_publication_retry_only_replays_frozen_failed_items(db):
    from juya_admin_api.modules.content.publication_adapter import build_publication_service
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.service import ContentService

    publication = build_publication_service(
        db.sessions,
        db.imports.stages,
        db.production,
        ContentService(SQLAlchemyContentRepository(db.sessions)),
        db.readings,
    )
    db.service.publication = publication
    order = await manual(db)
    second = await db.readings.command(
        "create",
        "new",
        db.actor,
        "second",
        {
            "series_id": db.series,
            "content": reading("Second"),
        },
    )
    second_item = uuid4().hex
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO content_work_item(public_id,work_order_id,position,content_id,origin"
                "al_revision_id) "
                "VALUES(:id,:order,2,:content,:revision)"
            ),
            {
                "id": second_item,
                "order": order.work_order_id,
                "content": second["content_id"],
                "revision": second["revision_id"],
            },
        )
    items = (await db.service.items(order.work_order_id)).items
    cohort = await db.proof.create(
        [
            {
                "content_type": "short_reading",
                "content_id": i.content_id,
                "revision_id": i.revision_id,
                "candidate_hash": i.candidate_hash,
            }
            for i in items
        ],
        db.actor,
    )
    for item in items:
        await db.proof.confirm(
            cohort["public_id"],
            item.revision_id,
            item.candidate_hash,
            db.actor,
            {"text_checked": True},
        )
    await db.proof.complete(cohort["public_id"], db.actor)
    preview = await db.service.command(
        order.work_order_id,
        "publication-preview",
        {
            "expected_version": order.version,
            "scope": "all",
            "selected_item_ids": [],
        },
        db.actor,
        "preview",
    )
    state = await db.service.publication_state(order.work_order_id, db.actor)
    receipt = await db.service.command(
        order.work_order_id,
        "publication-confirm",
        {
            "expected_version": preview.version,
            "scope": "all",
            "selected_item_ids": [],
            "publication_token": state.token,
            "range_version": state.version,
            "collection_hash": state.collection_hash,
            "manual_confirmed": True,
        },
        db.actor,
        "confirm",
    )
    publish = publication.publish_candidate
    calls = []

    async def fail_one(candidate, actor, key):
        calls.append(candidate["revision_id"])
        if candidate["revision_id"] == second["revision_id"]:
            raise AppError("TEST_TRANSIENT_PUBLISH_FAILED", "temporarily unavailable", 503)
        return await publish(candidate, actor, key)

    publication.publish_candidate = fail_one
    await publication.run(receipt.stage_id, "first-run")
    page = await db.service.items(order.work_order_id)
    assert page.counts.published == 1 and page.counts.failed == 1
    phases = await db.service.phases(order.work_order_id)
    phase = next(p for p in phases.phases if p.key == "publication")
    assert phase.counts.succeeded == phase.counts.failed == 1
    state = await db.service.publication_state(order.work_order_id, db.actor)
    publication.publish_candidate = publish
    retry = await db.service.command(
        order.work_order_id,
        "publication-retry",
        {
            "expected_version": receipt.version,
            "scope": "failed",
            "selected_item_ids": [],
            "publication_token": state.token,
            "range_version": state.version,
            "collection_hash": state.collection_hash,
            "manual_confirmed": True,
        },
        db.actor,
        "retry",
    )
    assert retry.selected_item_ids == [second_item]
    await publication.run(retry.stage_id, "retry-run")
    final = await db.service.publication_state(order.work_order_id, db.actor)
    assert [r.status for r in final.results] == ["SUCCEEDED", "SUCCEEDED"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unstarted_cancel_is_durable_and_original_import_cannot_resume_it(db):
    from tests.integration.test_v15_import_flow import import_zip

    created = await db.imports.create(
        series_id=db.series,
        content_type="dialogue",
        mode="ZIP",
        data=import_zip(uuid4().hex),
        actor=db.actor,
        key="upload",
    )
    async with db.sessions() as session:
        stage = await session.scalar(
            text("SELECT public_id FROM content_import_stage WHERE item_id=:id"),
            {"id": created["batch_id"]},
        )
    await db.imports.validate(created["batch_id"], stage)
    order = await db.service.from_batch(created["batch_id"], db.actor, "order")
    preview = await db.service.import_preview(order.work_order_id)
    await db.service.command(
        order.work_order_id,
        "cancel",
        {
            "expected_version": order.version,
            "scope": "all",
            "selected_item_ids": [],
        },
        db.actor,
        "cancel",
    )
    reloaded = await db.service.get(order.work_order_id)
    assert reloaded.status == "CANCELLED" and reloaded.counts.cancelled == 1
    with pytest.raises(AppError) as error:
        await db.imports.queue_confirmation(
            created["batch_id"],
            {
                "preview_hash": preview.preview_hash,
                "preview_version": preview.preview_version,
                "selected": [preview.items[0].import_item_id],
                "acknowledged_defaults": ["default_cover"],
                "confirmed_conflicts": [],
            },
            db.actor,
            "legacy-resume",
        )
    assert error.value.code == "IMPORT_CANCELLED"


@pytest.mark.asyncio
async def test_concurrent_candidate_change_invalidates_scope_and_blocks_order_edits(
    db, monkeypatch
):
    one = await manual(db)
    item = (await db.service.items(one.work_order_id)).items[0]
    two = await db.service.manual(
        series_id=db.series,
        content_type="short_reading",
        content_id=item.content_id,
        content=None,
        actor=db.actor,
        key="existing",
    )
    shared = (await db.service.items(two.work_order_id)).items[0]
    reserved, proceed = asyncio.Event(), asyncio.Event()
    create = db.proof.create

    async def paused_create(*args, **kwargs):
        reserved.set()
        await proceed.wait()
        return await create(*args, **kwargs)

    monkeypatch.setattr(db.proof, "create", paused_create)
    task = asyncio.create_task(
        db.service.command(
            one.work_order_id,
            "sampling",
            {
                "expected_version": one.version,
                "scope": "normal",
                "selected_item_ids": [],
            },
            db.actor,
            "sampling",
        )
    )
    await asyncio.wait_for(reserved.wait(), 10)
    payload = {
        "expected_version": one.version,
        "expected_revision_id": item.revision_id,
        "expected_edit_version": item.edit_version,
        "expected_content_version": item.content_version,
        "candidate_hash": item.candidate_hash,
        "content": reading("Concurrent change"),
    }
    with pytest.raises(AppError) as error:
        await db.service.correct(one.work_order_id, item.item_id, payload, db.actor, "blocked")
    assert error.value.code == "WORK_ORDER_COMMAND_PENDING"
    await db.service.correct(
        two.work_order_id,
        shared.item_id,
        payload | {"expected_version": two.version},
        db.actor,
        "other-order",
    )
    proceed.set()
    with pytest.raises(AppError) as error:
        await task
    assert error.value.code == "PROOFREADING_HASH_CHANGED"
    recovered = await db.service.get(one.work_order_id)
    assert recovered.version == one.version and recovered.pending_commands == []
    assert (
        await db.service.item(one.work_order_id, item.item_id)
    ).current.content.title_en == "Concurrent change"


@pytest.mark.asyncio
async def test_original_issue_acknowledgement_is_hash_bound_and_audited(db):
    order = await manual(db)
    item = (await db.service.items(order.work_order_id)).items[0]
    original = {"schema_version": 1, "blocks": [{"text": "Original English"}]}
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE content_work_item SET source_snapshot=:original,source_hash=:hash,histori"
                "cal_issues=:issues WHERE public_id=:id"
            ),
            {
                "id": item.item_id,
                "original": encode(original),
                "hash": snapshot_hash(original),
                "issues": encode(
                    [
                        {
                            "id": "parse-one",
                            "code": "IMPORT_PAIRING_REVIEW",
                            "message": "核对翻译",
                            "field": "translation_zh",
                        }
                    ]
                ),
            },
        )
    payload = {
        "expected_version": order.version,
        "expected_revision_id": item.revision_id,
        "expected_edit_version": item.edit_version,
        "expected_content_version": item.content_version,
        "candidate_hash": item.candidate_hash,
        "content": reading("Corrected"),
        "resolved_issue_ids": ["parse-one"],
    }
    with pytest.raises(AppError) as error:
        await db.service.correct(order.work_order_id, item.item_id, payload, db.actor, "no-compare")
    assert error.value.code == "WORK_ISSUE_ACK_INVALID"
    assert (
        await db.service.item(order.work_order_id, item.item_id)
    ).edit_version == item.edit_version
    await db.service.correct(
        order.work_order_id,
        item.item_id,
        payload | {"original_compared": True},
        db.actor,
        "compare",
    )
    corrected = await db.service.item(order.work_order_id, item.item_id)
    assert corrected.source_snapshot == original and corrected.historical_issues[0].resolved
    assert corrected.issues == []
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM audit_event WHERE action='work.correct' AND "
                    "actor_public_id=:actor"
                ),
                {"actor": db.actor},
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "SELECT candidate_hash FROM content_work_issue_resolution WHERE "
                    "work_item_id=:id"
                ),
                {"id": item.item_id},
            )
            == corrected.candidate_hash
        )
