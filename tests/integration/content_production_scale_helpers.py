"""Observe actual SQL projection and command scope for the real thousand import outcome."""

import json
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore
from juya_admin_api.modules.content_production.service import WorkOrderService
from juya_admin_api.shared.errors import AppError


def observe_queries(db):
    metrics = {"select_queries": 0, "central_matching_queries": 0, "central_rows_scanned": 0}
    engine = db.sessions.kw["bind"].sync_engine

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            metrics["select_queries"] += 1
        if "JOIN lexicon_active_sense a" in statement:
            metrics["central_matching_queries"] += 1
            assert cursor.rowcount >= 0
            metrics["central_rows_scanned"] += cursor.rowcount

    event.listen(engine, "after_cursor_execute", after_execute)
    return metrics, lambda: event.remove(engine, "after_cursor_execute", after_execute)


async def project_thousand(db, batch_id, central_metrics):
    proof = ProofreadingStore(db.sessions)
    service = WorkOrderService(
        db.sessions, db.service, db.service.production, db.service.readings, proof
    )
    metrics, remove = observe_queries(db)
    timings = {}
    try:
        started = perf_counter()
        order = await service.from_batch(batch_id, db.actor, "thousand-order")
        timings["register_seconds"] = perf_counter() - started
        metrics.update(select_queries=0, central_matching_queries=0, central_rows_scanned=0)
        started = perf_counter()
        first = await service.items(order.work_order_id)
        timings["page20_seconds"] = perf_counter() - started
        projection_queries = dict(metrics)
        assert first.total == first.counts.total == 1000
        assert len(first.items) == 20
        assert projection_queries["select_queries"] < 40000
        status = first.items[0].production_status
        started = perf_counter()
        all_items = []
        for page in range(1, 11):
            result = await service.items(order.work_order_id, page=page, page_size=100)
            assert result.total == result.counts.total == 1000
            assert len(result.items) == 100
            all_items.extend(result.items)
        timings["all_ten_pages_seconds"] = perf_counter() - started
        assert len({i.item_id for i in all_items}) == 1000
        assert len({i.content_id for i in all_items}) == 1000
        assert len({i.revision_id for i in all_items}) == 1000
        assert [i.item_id for i in all_items[:20]] == [i.item_id for i in first.items]
        filtered = await service.items(order.work_order_id, page=2, status=status)
        assert filtered.total == 1000 and len(filtered.items) == 20
        assert [i.item_id for i in filtered.items] == [i.item_id for i in all_items[20:40]]
        assert (await service.items(order.work_order_id, status="failed")).total == 0
        # The real command freezes every candidate, including those absent from page20.
        started = perf_counter()
        full = await service.command(
            order.work_order_id,
            "sampling",
            dict(expected_version=order.version, scope="all", selected_item_ids=[]),
            db.actor,
            "thousand-sample-all",
        )
        timings["sampling_all_seconds"] = perf_counter() - started
        cohort = await proof.get(full.proofreading_set_id)
        assert {c["revision_id"] for c in cohort["candidates"]} == {
            i.revision_id for i in all_items
        }
        assert len(full.selected_item_ids) == 1000
        assert len(cohort["sample_ids"]) <= 20
        selected = [i.item_id for i in first.items]
        small = await service.command(
            order.work_order_id,
            "sampling",
            dict(expected_version=full.version, scope="selected", selected_item_ids=selected),
            db.actor,
            "thousand-sample-page20",
        )
        small_cohort = await proof.get(small.proofreading_set_id)
        assert {c["revision_id"] for c in small_cohort["candidates"]} == {
            i.revision_id for i in first.items
        }
        assert small.selected_item_ids == selected
        with pytest.raises(AppError) as error:
            await service.command(
                order.work_order_id,
                "sampling",
                dict(
                    expected_version=small.version,
                    scope="selected",
                    selected_item_ids=[all_items[999].item_id, uuid4().hex],
                ),
                db.actor,
                "thousand-foreign-item",
            )
        assert error.value.code == "WORK_SELECTION_INVALID"
        async with db.sessions() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT COUNT(DISTINCT content_id),COUNT(DISTINCT original_revision_id) "
                        "FROM content_work_item WHERE work_order_id=:id"
                    ),
                    dict(id=order.work_order_id),
                )
            ).one()
            assert tuple(rows) == (1000, 1000)
        print(
            "TASK5_DISTINCT1000="
            + json.dumps(
                dict(
                    distinct_contents=1000,
                    distinct_revisions=1000,
                    page20=20,
                    all_pages=10,
                    central=central_metrics,
                    page20_projection_queries=projection_queries,
                    elapsed=timings,
                    total_observed_queries=metrics,
                ),
                sort_keys=True,
            )
        )
    finally:
        remove()
