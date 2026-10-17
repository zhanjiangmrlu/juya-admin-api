"""Actual InnoDB lock cycles using shared WHOLE and real FFmpeg slice facts."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.db.bounded_json import read_json
from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_work_orders import db as work_db
from tests.integration.test_v15_audio_proofreading_sql import prepared_cohort

db = work_db


async def shared_scene_order(db):
    # Reuse real bytes and owned slice lineage; choose IDs exposing cross-table lock order.
    _, _, actor, _, drafts, audio_ids = await prepared_cohort(db.sessions)
    db.proof.require_review = False
    first_slice, whole, last_slice = [prefix + uuid4().hex[:25] for prefix in "123"]
    async with db.sessions() as session, session.begin():
        for old, new in [
            (audio_ids[0][1], first_slice),
            (audio_ids[0][0], whole),
            (audio_ids[1][1], last_slice),
        ]:
            await session.execute(
                text("UPDATE audio_version SET public_id=:new WHERE public_id=:old"),
                {"new": new, "old": old},
            )
        source = deepcopy(drafts[0].content["audio"])
        source["version_id"] = whole
        source_sha = await session.scalar(
            text("SELECT sha256 FROM media_asset WHERE public_id=:id"),
            {"id": source["asset_id"]},
        )
        for draft, version, audio in zip(
            drafts[:2], [first_slice, last_slice], audio_ids[:2], strict=True
        ):
            content = deepcopy(draft.content)
            content["audio"] = source
            sentence = content["dialogue"][0]
            sentence["audio_version_id"] = whole
            sentence["slice_version"] = version
            digest = dependency_hash(
                sentence_id="sentence",
                english="Hello.",
                source_sha256=source_sha,
                source_audio_version=whole,
                boundary=Boundary(0, 1000),
                encoding_config=FFmpegSlicer.encoding_config,
            )
            sentence["slice_dependency_hash"] = digest
            draft.content = content
            await session.execute(
                text("UPDATE scene_revision SET content_snapshot=:snapshot WHERE public_id=:id"),
                {"snapshot": encode(content), "id": draft.id},
            )
            await session.execute(
                text(
                    "UPDATE sentence_audio_slice SET resource_version=:version,"
                    "source_audio_version=:whole,dependency_hash=:hash WHERE public_id=:id"
                ),
                {"version": version, "whole": whole, "hash": digest, "id": audio[2]},
            )
        series = await session.scalar(
            text(
                "SELECT cs.public_id FROM scene s JOIN content_series cs ON cs.id=s.series_id "
                "WHERE s.public_id=:id"
            ),
            {"id": drafts[0].scene_id},
        )
    candidates = [
        dict(
            content_type="dialogue",
            content_id=d.scene_id,
            revision_id=d.id,
            candidate_hash=snapshot_hash(d.content),
        )
        for d in drafts[:2]
    ]
    proof = await db.proof.create(candidates, actor)
    assert proof["normal_count"] == 2
    for candidate in candidates:
        await db.proof.confirm(
            proof["public_id"],
            candidate["revision_id"],
            candidate["candidate_hash"],
            actor,
            {"text_checked": True, "audio_listened": True},
        )
    await db.proof.complete(proof["public_id"], actor)
    orders = [
        await db.service.manual(
            series_id=series,
            content_type="dialogue",
            content_id=d.scene_id,
            content=None,
            actor=actor,
            key=uuid4().hex,
        )
        for d in drafts[:2]
    ]
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE content_work_item SET work_order_id=:target,position=2 "
                "WHERE work_order_id=:source"
            ),
            {"target": orders[0].work_order_id, "source": orders[1].work_order_id},
        )
    return orders[0].work_order_id, first_slice, whole


@pytest.mark.asyncio
@pytest.mark.parametrize("simultaneous_requests", [False, True])
async def test_scene_projection_shared_audio_reads_do_not_deadlock(
    db, monkeypatch, simultaneous_requests
):
    order_id, first_slice, whole = await shared_scene_order(db)
    effects_sql = text(
        "SELECT (SELECT COUNT(*) FROM audit_event),"
        "(SELECT COUNT(*) FROM content_import_stage),"
        "(SELECT COUNT(*) FROM proofreading_confirmation)"
    )
    async with db.sessions() as session:
        before = (await session.execute(effects_sql)).one()
    execute = AsyncSession.execute
    barrier = asyncio.Barrier(2)
    reached: set[int] = set()
    lock_queries: list[str] = []

    async def scheduled(session, statement, params=None, *args, **kwargs):
        sql = str(statement)
        audio_join = "FROM audio_version v JOIN audio_target t" in sql
        identity = (params or {}).get("id")
        if audio_join and "FOR UPDATE" in sql:
            lock_queries.append(sql)
        if simultaneous_requests and audio_join and identity == first_slice and len(reached) < 2:
            reached.add(id(session))
            await asyncio.wait_for(barrier.wait(), 10)
        result = await execute(session, statement, params, *args, **kwargs)
        if (
            not simultaneous_requests
            and audio_join
            and identity in {first_slice, whole}
            and id(session) not in reached
            and len(reached) < 2
        ):
            reached.add(id(session))
            await asyncio.wait_for(barrier.wait(), 10)
        return result

    monkeypatch.setattr(AsyncSession, "execute", scheduled)
    calls = [db.service.items(order_id)]
    if simultaneous_requests:
        calls.append(db.service.get(order_id))
    results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 20)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, [(type(e).__name__, str(e)) for e in failures]
    assert len(reached) == 2
    assert results[0].total == 2
    assert all(i.publishable for i in results[0].items)
    assert not lock_queries, "observational scene projection must not acquire media write locks"
    async with db.sessions() as session:
        assert (await session.execute(effects_sql)).one() == before


@pytest.mark.asyncio
async def test_observation_does_not_relax_write_locks_or_changed_resource_publication(db):
    order_id, _, _ = await shared_scene_order(db)
    item = (await db.service.items(order_id)).items[0]
    async with db.sessions() as session, session.begin():
        snapshot = await read_json(
            session, "scene_revision", "content_snapshot", {"public_id": item.revision_id}
        )
        observed = await db.proof.observation_facts(session, "dialogue", item.revision_id, snapshot)
        assert observed["ready"] and observed["normal"]
    async with db.sessions() as writer, writer.begin():
        locked = await db.proof.facts(writer, "dialogue", item.revision_id, snapshot)
        assert locked == observed
        # Independent real SQL transactions cannot take these write-guarded resources.
        for table, identity in [
            ("media_asset", snapshot["audio"]["asset_id"]),
            ("audio_version", snapshot["audio"]["version_id"]),
            ("scene_revision", item.revision_id),
        ]:
            async with db.sessions() as contender, contender.begin():
                with pytest.raises(OperationalError) as blocked:
                    await contender.execute(
                        text(
                            f"SELECT public_id FROM {table} WHERE public_id=:id FOR UPDATE NOWAIT"
                        ),
                        {"id": identity},
                    )
                assert blocked.value.orig.args[0] == 3572
    async with db.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE media_asset SET security_status='BLOCKED' WHERE public_id=:id"),
            {"id": snapshot["audio"]["asset_id"]},
        )
    async with db.sessions() as session, session.begin():
        after = await db.proof.observation_facts(session, "dialogue", item.revision_id, snapshot)
        assert not after["ready"] and "AUDIO_MISSING" in after["error_codes"]
    # A previous good observational result never authorizes mutation or publication.
    state = await db.proof.create(
        [
            dict(
                content_type="dialogue",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    with pytest.raises(AppError) as denied:
        await db.proof.confirm(
            state["public_id"],
            item.revision_id,
            item.candidate_hash,
            db.actor,
            {"text_checked": True, "audio_listened": True},
        )
    assert denied.value.code == "PROOFREADING_CHECK_REQUIRED"
    db.production.require_review = False
    db.production.check_confirmation = db.proof.is_confirmed_within
    with pytest.raises(AppError) as refused:
        await db.production.publish(
            SceneRevision(
                item.revision_id, item.content_id, None, version=item.edit_version, content=snapshot
            ),
            db.actor,
            uuid4().hex,
            datetime.now(UTC),
            expected_candidate_hash=item.candidate_hash,
        )
    assert refused.value.code == "PUBLISH_CHECK_FAILED"
    assert "AUDIO_MISSING" in refused.value.details["error_codes"]
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT published_revision_id FROM scene WHERE public_id=:id"),
                {"id": item.content_id},
            )
            is None
        )
