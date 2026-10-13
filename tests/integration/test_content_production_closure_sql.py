"""Real migrated MySQL regressions for production review closure."""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import text

from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_work_orders import db as work_order_db
from tests.integration.test_content_work_orders import manual, reading
from tests.integration.test_v15_audio_authoring_flow import generated_wav

db = work_order_db


async def prepared_reading(db, *, audio=True):
    order = await manual(db, uuid4().hex)
    item = (await db.service.items(order.work_order_id)).items[0]
    content = reading()
    if audio:
        asset, target, version = [uuid4().hex[:26] for _ in range(3)]
        data = generated_wav(440 + int(uuid4().hex[:6], 16))
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO media_asset(public_id,object_key,asset_type,content_type,"
                    "size_bytes,sha256,status,security_status,created_by,created_at,duration_ms) "
                    "VALUES(:id,:key,'audio','audio/wav',:size,:sha,'CONFIRMED','PASSED',"
                    ":actor,UTC_TIMESTAMP(6),3000)"
                ),
                dict(
                    id=asset,
                    key="sealed/media/audio/" + asset,
                    size=len(data),
                    sha=hashlib.sha256(data).hexdigest(),
                    actor=db.actor,
                ),
            )
            await session.execute(
                text(
                    "INSERT INTO audio_target(public_id,stable"
                    "_key,target_type) VALUES(:id,:key,'FULL')"
                ),
                dict(id=target, key=f"short-reading:{item.content_id}:{item.revision_id}:FULL"),
            )
            await session.execute(
                text(
                    "INSERT INTO audio_version(public_id,targ"
                    "et_id,asset_id,version_no,source,status,"
                    "created_by,created_at) SELECT :id,t.id,m.id,1,'MANUAL','READY',:actor,"
                    "UTC_TIMESTAMP(6) FROM audio_target t,media_asset m "
                    "WHERE t.public_id=:target AND m.public_id=:asset"
                ),
                dict(id=version, actor=db.actor, target=target, asset=asset),
            )
        content["audio"] = dict(
            asset_id=asset, target_id=target, version_id=version, duration_ms=3000
        )
    await db.service.correct(
        order.work_order_id,
        item.item_id,
        dict(
            expected_version=order.version,
            expected_revision_id=item.revision_id,
            expected_edit_version=item.edit_version,
            expected_content_version=item.content_version,
            candidate_hash=item.candidate_hash,
            content=content,
        ),
        db.actor,
        uuid4().hex,
    )
    item = await db.service.item(order.work_order_id, item.item_id)
    return order, item


@pytest.mark.asyncio
async def test_ready_reading_audio_sample_bulk_complete_and_publish(db):
    pairs = [await prepared_reading(db) for _ in range(3)]
    items = [item for _, item in pairs]
    candidates = [
        dict(
            content_type="short_reading",
            content_id=i.content_id,
            revision_id=i.revision_id,
            candidate_hash=i.candidate_hash,
        )
        for i in items
    ]
    cohort = await db.proof.create(candidates, db.actor)
    assert cohort["normal_count"] == 3
    sample = next(c for c in cohort["candidates"] if c["revision_id"] in cohort["sample_ids"])
    with pytest.raises(AppError):
        await db.proof.confirm(
            cohort["public_id"],
            sample["revision_id"],
            sample["candidate_hash"],
            db.actor,
            {"text_checked": True},
        )
    await db.proof.confirm(
        cohort["public_id"],
        sample["revision_id"],
        sample["candidate_hash"],
        db.actor,
        {"text_checked": True, "audio_listened": True},
    )
    refreshed = await db.proof.get(cohort["public_id"])
    assert refreshed["collection_hash"] != cohort["collection_hash"]
    await db.proof.bulk_confirm(
        cohort["public_id"],
        db.actor,
        refreshed["collection_hash"],
        [i.revision_id for i in items],
        True,
        uuid4().hex,
    )
    await db.proof.complete(cohort["public_id"], db.actor)
    for order, item in pairs:
        current = await db.service.item(order.work_order_id, item.item_id)
        assert current.publishable
        assert current.current.content.audio.listening_confirmed
        async with db.sessions() as session, session.begin():
            revision = await db.readings.revision(session, item.content_id, item.revision_id)
            assert await db.readings.inspect(session, revision) == []
        await db.readings.command(
            "publish",
            item.content_id,
            db.actor,
            uuid4().hex,
            dict(
                expected_version=current.content_version,
                revision_id=item.revision_id,
                manual_confirmed=True,
            ),
        )


@pytest.mark.asyncio
async def test_reading_wrong_full_owner_never_normal(db):
    order, item = await prepared_reading(db)
    async with db.sessions() as session, session.begin():
        await session.execute(
            text("UPDATE audio_target SET stable_key='foreign:FULL' WHERE public_id=:id"),
            dict(id=item.current.content.audio.target_id),
        )
    candidate = await db.service.item(order.work_order_id, item.item_id)
    assert candidate.production_status == "review_required"
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    assert cohort["normal_count"] == 0


async def sample_reading(db, item):
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    assert cohort["normal_count"] == 1
    await db.proof.confirm(
        cohort["public_id"],
        item.revision_id,
        item.candidate_hash,
        db.actor,
        {"text_checked": True, "audio_listened": True},
    )
    await db.proof.complete(cohort["public_id"], db.actor)


@pytest.mark.asyncio
async def test_new_revision_reuses_exact_owned_full_through_source_chain_and_resamples(db):
    order, original = await prepared_reading(db)
    await sample_reading(db, original)
    original = await db.service.item(order.work_order_id, original.item_id)
    published = await db.readings.command(
        "publish",
        original.content_id,
        db.actor,
        uuid4().hex,
        dict(
            expected_version=original.content_version,
            revision_id=original.revision_id,
            manual_confirmed=True,
        ),
    )
    await db.readings.command(
        "revision",
        original.content_id,
        db.actor,
        uuid4().hex,
        dict(expected_version=published["version"], source_revision_id=original.revision_id),
    )
    middle = await db.service.item(order.work_order_id, original.item_id)
    await db.readings.command(
        "revision",
        middle.content_id,
        db.actor,
        uuid4().hex,
        dict(expected_version=middle.content_version, source_revision_id=middle.revision_id),
    )
    copied = await db.service.item(order.work_order_id, original.item_id)
    assert copied.revision_id != original.revision_id
    assert copied.current.content.audio == original.current.content.audio
    assert copied.production_status == "normal" and not copied.confirmed
    await sample_reading(db, copied)
    current = await db.service.item(order.work_order_id, original.item_id)
    assert current.publishable
    async with db.sessions() as session:
        saved_original = await db.readings.revision(
            session, original.content_id, original.revision_id
        )
        assert snapshot_hash(saved_original["content_snapshot"]) == original.candidate_hash
        assert saved_original["status"] == "PUBLISHED"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign_chain", "changed_reference"])
async def test_reading_source_chain_cannot_authorize_foreign_or_changed_full(db, change):
    order, original = await prepared_reading(db)
    _other_order, other = await prepared_reading(db)
    await db.readings.command(
        "revision",
        original.content_id,
        db.actor,
        uuid4().hex,
        dict(expected_version=original.content_version, source_revision_id=original.revision_id),
    )
    copied = await db.service.item(order.work_order_id, original.item_id)
    if change == "foreign_chain":
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE short_reading_revision SET source_revision_id=:source "
                    "WHERE public_id=:id"
                ),
                dict(source=other.revision_id, id=copied.revision_id),
            )
    else:
        # Even a same-content-looking target key cannot replace the exact source reference.
        async with db.sessions() as session, session.begin():
            await session.execute(
                text("UPDATE audio_target SET stable_key=:key WHERE public_id=:id"),
                dict(
                    key=f"short-reading:{original.content_id}:{other.revision_id}:FULL",
                    id=other.current.content.audio.target_id,
                ),
            )
        content = copied.current.content.model_dump(mode="json")
        content["audio"] = other.current.content.audio.model_dump(mode="json")
        await db.readings.command(
            "save",
            copied.content_id,
            db.actor,
            uuid4().hex,
            dict(
                expected_version=copied.content_version,
                revision_id=copied.revision_id,
                expected_edit_version=copied.edit_version,
                content=content,
            ),
        )
    current = await db.service.item(order.work_order_id, original.item_id)
    assert current.production_status == "review_required"
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=current.content_id,
                revision_id=current.revision_id,
                candidate_hash=current.candidate_hash,
            )
        ],
        db.actor,
    )
    assert cohort["normal_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sha256", "duration_ms", "security_status"])
async def test_reading_resource_change_after_sample_revokes_completion(db, change):
    _order, item = await prepared_reading(db)
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    await db.proof.confirm(
        cohort["public_id"],
        item.revision_id,
        item.candidate_hash,
        db.actor,
        {"text_checked": True, "audio_listened": True},
    )
    value = {"sha256": "f" * 64, "duration_ms": 2999, "security_status": "BLOCKED"}[change]
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(f"UPDATE media_asset SET {change}=:value WHERE public_id=:id"),
            {"id": item.current.content.audio.asset_id, "value": value},
        )
    with pytest.raises(AppError) as denied:
        await db.proof.complete(cohort["public_id"], db.actor)
    assert denied.value.code in {"PROOFREADING_MATERIAL_CHANGED", "PROOFREADING_CHECK_REQUIRED"}


@pytest.mark.asyncio
async def test_reading_without_audio_needs_no_listening(db):
    order, item = await prepared_reading(db, audio=False)
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    await db.proof.confirm(
        cohort["public_id"], item.revision_id, item.candidate_hash, db.actor, {"text_checked": True}
    )
    await db.proof.complete(cohort["public_id"], db.actor)
    assert (await db.service.item(order.work_order_id, item.item_id)).publishable


@pytest.mark.asyncio
async def test_reading_confirmation_recovers_original_receipt_after_its_own_hash_change(db):
    _order, item = await prepared_reading(db)
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=item.content_id,
                revision_id=item.revision_id,
                candidate_hash=item.candidate_hash,
            )
        ],
        db.actor,
    )
    args = (
        cohort["public_id"],
        item.revision_id,
        item.candidate_hash,
        db.actor,
        {"text_checked": True, "audio_listened": True},
    )
    await db.proof.confirm(*args, idempotency_key="listen-once")
    state = await db.proof.get(cohort["public_id"])
    await db.proof.confirm(*args, idempotency_key="listen-once")
    assert (await db.proof.get(cohort["public_id"])) == state


@pytest.mark.asyncio
async def test_resolved_original_pairing_reaches_sampling_and_survives_listening(db):
    order, item = await prepared_reading(db)
    original = {"blocks": [{"text": "Hello."}, {"text": "你好。"}]}
    diagnostic = dict(
        id="pairing", code="BILINGUAL_PAIRING_REQUIRED", field="dialogue", message="Compare"
    )
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "UPDATE content_work_item SET source_snapshot=:source,source_hash=:hash,"
                "historical_issues=:issues WHERE public_id=:id"
            ),
            dict(
                source=encode(original),
                hash=snapshot_hash(original),
                issues=encode([diagnostic]),
                id=item.item_id,
            ),
        )
    correction = dict(
        expected_version=order.version,
        expected_revision_id=item.revision_id,
        expected_edit_version=item.edit_version,
        expected_content_version=item.content_version,
        candidate_hash=item.candidate_hash,
        content=item.current.content.model_dump(mode="json"),
        resolved_issue_ids=["pairing"],
        original_compared=True,
    )
    await db.service.correct(order.work_order_id, item.item_id, correction, db.actor, uuid4().hex)
    fixed = await db.service.item(order.work_order_id, item.item_id)
    assert fixed.production_status == "normal"
    cohort = await db.proof.create(
        [
            dict(
                content_type="short_reading",
                content_id=fixed.content_id,
                revision_id=fixed.revision_id,
                candidate_hash=fixed.candidate_hash,
            )
        ],
        db.actor,
    )
    assert cohort["normal_count"] == 1
    await db.proof.confirm(
        cohort["public_id"],
        fixed.revision_id,
        fixed.candidate_hash,
        db.actor,
        {"text_checked": True, "audio_listened": True},
    )
    listened = await db.service.item(order.work_order_id, item.item_id)
    assert not any(i.id == "pairing" for i in listened.issues)
    assert listened.historical_issues[0].resolved
    await db.service.correct(
        order.work_order_id,
        item.item_id,
        correction
        | dict(
            expected_edit_version=listened.edit_version,
            expected_content_version=listened.content_version,
            candidate_hash=listened.candidate_hash,
            content=listened.current.content.model_dump(mode="json")
            | {"translation_zh": "不相关译文"},
            resolved_issue_ids=[],
            original_compared=False,
        ),
        db.actor,
        uuid4().hex,
    )
    changed = await db.service.item(order.work_order_id, item.item_id)
    assert any(i.id == "pairing" for i in changed.issues)
