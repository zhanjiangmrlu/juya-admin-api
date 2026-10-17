"""Real SQL counterexamples from Task 3 scoped review round one."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.modules.content_production.effective_issues import current_issues_within
from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_production_candidates_sql import Suggestions, candidate
from tests.integration.test_content_work_orders import db as work_order_db

db = work_order_db


async def card(db, english="bank-review"):
    from juya_admin_api.shared.ids import new_ulid

    scene = await db.production.create_scene(db.series, "word_card")
    return await db.production.save_revision(
        SceneRevision(
            new_ulid(datetime.now(UTC)),
            scene,
            None,
            content=dict(
                schema_version=2,
                template_type="word_card",
                title_en="Bank",
                title_zh="河岸",
                copyright="Owned",
                source="Fixture",
                dialogue=[
                    dict(id="one", english="A bank here.", chinese="河岸在这里"),
                    dict(id="two", english="The bank there.", chinese="银行在那里"),
                ],
                vocabulary=[
                    dict(
                        english=english,
                        chinese="河岸",
                        source_sentence_ids=["one"],
                        source_sentence="A bank here.",
                        source_locator={"sentence_id": "one", "start": "2", "end": "6"},
                    )
                ],
            ),
        ),
        None,
    )


async def change_entry(db, draft, **changes):
    from copy import deepcopy

    body = deepcopy(draft.content)
    body["vocabulary"][0].update(changes)
    return await db.production.save_revision(
        SceneRevision(draft.id, draft.scene_id, None, content=body, created_by=db.actor),
        draft.version,
    )


@pytest.mark.asyncio
async def test_semantic_replacement_creates_new_entry_sense_source_and_scoped_import_key(db):
    first = await card(db)
    original = first.content["vocabulary"][0]
    second = await change_entry(db, first, chinese="银行", meaning_change="new_sense")
    changed = second.content["vocabulary"][0]
    assert all(changed[field] != original[field] for field in ("entry_id", "sense_id", "source_id"))
    async with db.sessions() as session:
        mappings = (
            (
                await session.execute(
                    text(
                        "SELECT namespace,content_type,content_id,import_entry_id,entry_id "
                        "FROM import_entry_mapping WHERE entry_id IN (:old,:new)"
                    ),
                    dict(old=original["entry_id"], new=changed["entry_id"]),
                )
            )
            .mappings()
            .all()
        )
        assert len(mappings) == 2 and len({r["import_entry_id"] for r in mappings}) == 2
        audit = (
            await session.execute(
                text(
                    "SELECT actor_public_id,after_summary FROM audit_event "
                    "WHERE action='entry.identity.replace' AND object_public_id=:id"
                ),
                dict(id=first.id),
            )
        ).one()
        from juya_admin_api.modules.content.production_store import decode

        evidence = decode(audit.after_summary)
        assert audit.actor_public_id == db.actor and evidence["reason"] == "new_sense"
        assert evidence["new_entry_version"] == changed["entry_version"]

        assert all(
            r["content_id"] == first.scene_id and r["namespace"] == "juya-content-library"
            for r in mappings
        )
        assert (
            await session.scalar(
                text("SELECT sense_id FROM content_entry WHERE public_id=:id"),
                {"id": original["entry_id"]},
            )
            == original["sense_id"]
        )


@pytest.mark.asyncio
async def test_same_source_wording_and_offsets_keep_identity_but_real_target_move_forks(db):
    first = await card(db)
    original = first.content["vocabulary"][0]
    wording = await change_entry(
        db,
        first,
        chinese="河边的岸",
        meaning_change="wording",
        source_sentence="A river bank here.",
        source_locator={"sentence_id": "one", "start": "8", "end": "12"},
    )
    corrected = wording.content["vocabulary"][0]
    assert all(
        corrected[field] == original[field] for field in ("entry_id", "sense_id", "source_id")
    )
    saved_again = await change_entry(db, wording, explanation="同一来源继续完善")
    moved = await change_entry(
        db,
        saved_again,
        source_sentence_ids=["two"],
        source_sentence="The bank there.",
        source_locator={"sentence_id": "two", "start": "4", "end": "8"},
    )
    assert all(
        moved.content["vocabulary"][0][field] != original[field]
        for field in ("entry_id", "sense_id", "source_id")
    )


@pytest.mark.asyncio
async def test_central_definition_binding_is_fixed_to_published_entry_revision(db):
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.schemas import SceneEntry
    from juya_admin_api.modules.content.service import ContentService
    from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
    from juya_admin_api.modules.content_production.candidates import decide_provided
    from juya_admin_api.shared.ids import new_ulid

    first = await card(db)
    order = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=first.scene_id,
        content=None,
        actor=db.actor,
        key="version-binding",
    )
    group = (await db.service.candidate_groups(order.work_order_id)).groups[0]
    await decide_provided(
        db.service,
        group.candidate_id,
        [
            dict(
                result_id=group.results[0].result_id,
                decision="KEEP_PROVIDED",
                central_action="new_sense",
            )
        ],
        first.version,
        db.actor,
        "bind-a",
    )
    async with db.sessions() as session:
        binding = (
            await session.execute(
                text(
                    "SELECT lexicon_entry_id,lexicon_entry_version FROM "
                    "content_entry_revision WHERE revision_id=:revision AND entry_id=:entry"
                ),
                dict(revision=first.id, entry=first.content["vocabulary"][0]["entry_id"]),
            )
        ).one()
        assert binding.lexicon_entry_id and binding.lexicon_entry_version == 1
    await db.service.authoring.confirm_entry(
        first.id, first.content["vocabulary"][0]["entry_id"], first.version, db.actor, True
    )
    content = ContentService(SQLAlchemyContentRepository(db.sessions))
    first = await content.get_revision(first.id)
    cohort = await db.proof.create(
        [
            dict(
                content_type="word_card",
                content_id=first.scene_id,
                revision_id=first.id,
                candidate_hash=snapshot_hash(first.content),
            )
        ],
        db.actor,
    )
    await db.proof.confirm(
        cohort["public_id"],
        first.id,
        snapshot_hash(first.content),
        db.actor,
        {"text_checked": True},
    )
    await db.proof.complete(cohort["public_id"], db.actor)
    db.production.check_confirmation = db.proof.is_confirmed_within
    await db.production.publish(first, db.actor, "publish-a", datetime.now(UTC))
    second = await db.production.save_revision(
        SceneRevision(new_ulid(datetime.now(UTC)), first.scene_id, first.id, content=first.content),
        None,
    )
    second = await change_entry(db, second, chinese="河边的岸", meaning_change="wording")
    central = await db.production.write_entry(
        SceneEntry(
            entry_id=binding.lexicon_entry_id,
            entry_version=1,
            english="bank-review",
            chinese="河边的岸",
            meaning_change="wording",
        ),
        "VOCABULARY",
        db.actor,
    )
    from juya_admin_api.modules.content.schemas import SceneContent
    from juya_admin_api.modules.content_production.lexicon import organize_within

    async with db.sessions() as session, session.begin():
        await organize_within(
            session,
            second.id,
            second.scene_id,
            SceneContent.model_validate(second.content),
            db.actor,
        )
    group = (await db.service.candidate_groups(order.work_order_id)).groups[0]
    await decide_provided(
        db.service,
        group.candidate_id,
        [dict(result_id=group.results[0].result_id, decision="KEEP_PROVIDED")],
        second.version,
        db.actor,
        "bind-b",
    )
    async with db.sessions() as session:
        bindings = (
            (
                await session.execute(
                    text(
                        "SELECT revision_id,lexicon_entry_id,lexicon_entry_version "
                        "FROM content_entry_revision WHERE revision_id IN (:a,:b)"
                    ),
                    dict(a=first.id, b=second.id),
                )
            )
            .mappings()
            .all()
        )
        versions = {r["revision_id"]: r["lexicon_entry_version"] for r in bindings}
        assert versions == {first.id: 1, second.id: central.entry_version}
    assert (await content.get_revision(first.id)).content == first.content
    from juya_admin_api.modules.content_production.entry_bindings import read

    item = (await db.service.items(order.work_order_id)).items[0]
    published_binding = await read(db.service, order.work_order_id, item.item_id, first.id)
    current_binding = await read(db.service, order.work_order_id, item.item_id, second.id)
    assert published_binding.bindings[0].definition_version == 1
    assert current_binding.bindings[0].definition_version == central.entry_version


async def pending_issues(db, draft):
    async with db.sessions() as session:
        return await current_issues_within(session, draft.scene_id, draft.id, draft.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed_empty", "config_stale"])
async def test_optional_ai_group_can_be_dismissed_without_results_or_current_config(db, state):
    service, identifier, draft = await candidate(db)
    if state == "failed_empty":
        row = await service.get(identifier)
        payload = row["candidate"] | {"results": []}
        async with db.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE lexicon_candidate SET status='FAILED',candidate=:p WHERE public_id=:id"
                ),
                dict(id=identifier, p=encode(payload)),
            )
    else:
        service.config_version = "v2"
    assert await pending_issues(db, draft)
    receipt = await service.dismiss(
        identifier, draft.version, db.actor, "skip-optional", "不采用推荐"
    )
    assert receipt["status"] == "DISMISSED"
    assert (
        await service.dismiss(identifier, draft.version, db.actor, "skip-optional", "不采用推荐")
        == receipt
    )
    assert not await pending_issues(db, draft)
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM content_cloud_attempt WHERE stage_id=:id"),
                {"id": identifier},
            )
            == 1
        )
    with pytest.raises(AppError) as denied:
        await service.decide(
            identifier,
            [dict(result_id="unused", decision="ADOPTED")],
            draft.version,
            db.actor,
            "adopt-dismissed",
        )
    assert denied.value.code == "CANDIDATE_DISMISSED"


@pytest.mark.asyncio
async def test_unknown_dismissal_preserves_unknown_charge_and_cannot_enqueue_new_config(db):
    service, _old, original = await candidate(db)
    draft = await db.production.save_revision(
        SceneRevision(
            original.id,
            original.scene_id,
            None,
            content=original.content | {"title_en": "Unknown outcome"},
        ),
        original.version,
    )

    class Unknown(Suggestions):
        async def suggest(self, *args, **kwargs):
            raise RuntimeError("deterministic provider timeout after submission")

    service.ai = Unknown()
    identifier = await service.queue(
        draft.id, "AI_LEXICON", draft.version, db.actor, datetime.now(UTC)
    )
    with pytest.raises(RuntimeError):
        await service.run(identifier, uuid4().hex, datetime.now(UTC))
    await service.dismiss(
        identifier, draft.version, db.actor, "dismiss-unknown", "保留未知账单但放弃推荐"
    )
    assert not await pending_issues(db, draft)
    async with db.sessions() as session:
        attempt = (
            await session.execute(
                text("SELECT status,actual_units FROM content_cloud_attempt WHERE stage_id=:id"),
                {"id": identifier},
            )
        ).one()
        assert attempt.status == "UNKNOWN" and attempt.actual_units is None
    service.config_version = "v2"
    with pytest.raises(AppError) as denied:
        await service.queue(draft.id, "AI_LEXICON", draft.version, db.actor, datetime.now(UTC))
    assert denied.value.code == "PROVIDER_OUTCOME_UNKNOWN"


@pytest.mark.asyncio
async def test_dismissed_inflight_group_stays_dismissed_when_actual_result_arrives(db):
    service, _old, original = await candidate(db)
    draft = await db.production.save_revision(
        SceneRevision(
            original.id,
            original.scene_id,
            None,
            content=original.content | {"title_en": "Inflight outcome"},
        ),
        original.version,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Delayed(Suggestions):
        async def suggest(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super().suggest(*args, **kwargs)

    service.ai = Delayed()
    identifier = await service.queue(
        draft.id, "AI_LEXICON", draft.version, db.actor, datetime.now(UTC)
    )
    running = asyncio.create_task(service.run(identifier, uuid4().hex, datetime.now(UTC)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await service.dismiss(identifier, draft.version, db.actor, "dismiss-inflight", "不采用")
    finally:
        release.set()
        await running
    assert not await pending_issues(db, draft)
    async with db.sessions() as session:
        attempt = (
            await session.execute(
                text("SELECT status,actual_units FROM content_cloud_attempt WHERE stage_id=:id"),
                {"id": identifier},
            )
        ).one()
        assert attempt.status == "SUCCEEDED" and attempt.actual_units == 20
    assert (await service.get(identifier))["dismissed_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change,meaning", [("new_sense", "银行"), ("wording", "河边的岸")])
async def test_explicit_central_meaning_selection_forks_only_semantic_replacement(
    db, change, meaning
):
    from juya_admin_api.modules.content.schemas import SceneEntry
    from juya_admin_api.modules.content_production.candidates import decide_provided
    from juya_admin_api.modules.content_production.entry_bindings import read

    central = await db.production.write_entry(
        SceneEntry(english="bank-" + change, chinese=meaning), "VOCABULARY", db.actor
    )
    draft = await card(db, english="bank-" + change)
    old = draft.content["vocabulary"][0]
    order = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=draft.scene_id,
        content=None,
        actor=db.actor,
        key="central-identity",
    )
    item = (await db.service.items(order.work_order_id)).items[0]
    original = (await read(db.service, order.work_order_id, item.item_id)).bindings[0]
    group = (await db.service.candidate_groups(order.work_order_id)).groups[0]
    decision = dict(
        result_id=group.results[0].result_id,
        decision="USE_CENTRAL",
        lexicon_entry_id=central.entry_id,
        definition_version=central.entry_version,
    )
    with pytest.raises(AppError) as ambiguous:
        await decide_provided(
            db.service, group.candidate_id, [decision], draft.version, db.actor, "ambiguous"
        )
    assert ambiguous.value.code == "MEANING_CHANGE_CONFIRMATION_REQUIRED"
    await decide_provided(
        db.service,
        group.candidate_id,
        [decision | dict(meaning_change=change)],
        draft.version,
        db.actor,
        "explicit-central",
    )
    binding = (await read(db.service, order.work_order_id, item.item_id)).bindings[0]
    assert (
        binding.lexicon_entry_id == central.entry_id
        and binding.definition_version == central.entry_version
    )
    assert binding.namespace == "juya-content-library" and binding.content_id == draft.scene_id
    for field in ("entry_id", "sense_id", "source_id"):
        assert (getattr(binding, field) == old[field]) == (change == "wording")
    assert (binding.import_entry_id == original.import_entry_id) == (change == "wording")
    async with db.sessions() as session:
        assert await session.scalar(
            text("SELECT COUNT(*) FROM import_entry_mapping WHERE content_id=:content"),
            dict(content=draft.scene_id),
        ) == (2 if change == "new_sense" else 1)


@pytest.mark.asyncio
async def test_document_block_paragraph_targets_are_identity_but_text_offsets_are_not(db):
    first = await card(db)
    anchored = await change_entry(
        db,
        first,
        source_sentence_ids=[],
        source_locator={
            "document_path": "one.docx",
            "block_id": "body",
            "paragraph_id": "p1",
            "start": "2",
        },
    )
    original = anchored.content["vocabulary"][0]
    corrected = await change_entry(
        db,
        anchored,
        source_sentence="Adjusted source text",
        source_locator={
            "document_path": "one.docx",
            "block_id": "body",
            "paragraph_id": "p1",
            "start": "4",
        },
    )
    assert corrected.content["vocabulary"][0]["entry_id"] == original["entry_id"]
    moved = await change_entry(
        db,
        corrected,
        source_locator={
            "document_path": "one.docx",
            "block_id": "body",
            "paragraph_id": "p2",
            "start": "4",
        },
    )
    assert all(
        moved.content["vocabulary"][0][field] != original[field]
        for field in ("entry_id", "sense_id", "source_id")
    )


@pytest.mark.asyncio
async def test_scoped_dismissal_replays_and_unsubmitted_work_never_calls_provider(db):
    from dataclasses import replace

    service, _, draft = await candidate(db)
    db.service.candidates = service
    draft.content["title_en"] = "New recommendation dependency"
    draft = await db.production.save_revision(replace(draft, created_by=db.actor), draft.version)
    identifier = await service.queue(
        draft.id, "AI_LEXICON", draft.version, db.actor, datetime.now(UTC)
    )
    order = await db.service.manual(
        series_id=db.series,
        content_type="dialogue",
        content_id=draft.scene_id,
        content=None,
        actor=db.actor,
        key="dismiss-order",
    )
    item = (await db.service.items(order.work_order_id)).items[0]
    payload = dict(
        expected_version=order.version,
        scope="selected",
        selected_item_ids=[item.item_id],
        candidate_id=identifier,
        expected_edit_version=draft.version,
        reason="skip before submission",
    )
    receipt = await db.service.command(
        order.work_order_id, "candidate-dismissals", payload, db.actor, "skip"
    )
    assert receipt.status == "DISMISSED"
    assert (
        await db.service.command(
            order.work_order_id, "candidate-dismissals", payload, db.actor, "skip"
        )
        == receipt
    )
    with pytest.raises(AppError) as stopped:
        await service.run(identifier, uuid4().hex, datetime.now(UTC))
    assert stopped.value.code == "CANDIDATE_DISMISSED"
    assert not await pending_issues(db, draft)
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT COUNT(*) FROM content_cloud_attempt WHERE stage_id=:id"),
                dict(id=identifier),
            )
            == 0
        )
        assert (
            await session.scalar(
                text(
                    "SELECT COUNT(*) FROM audit_event WHERE action='work.ai.dismiss' AND "
                    "object_public_id=:id"
                ),
                dict(id=draft.id),
            )
            == 1
        )
    groups = (await db.service.candidate_groups(order.work_order_id)).groups
    assert next(g for g in groups if g.candidate_id == identifier).dismissed
    other = await card(db)
    foreign = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=other.scene_id,
        content=None,
        actor=db.actor,
        key="foreign-order",
    )
    foreign_item = (await db.service.items(foreign.work_order_id)).items[0]
    with pytest.raises(AppError) as denied:
        await db.service.command(
            foreign.work_order_id,
            "candidate-dismissals",
            payload
            | dict(expected_version=foreign.version, selected_item_ids=[foreign_item.item_id]),
            db.actor,
            "foreign",
        )
    assert denied.value.code == "WORK_CANDIDATE_SCOPE_INVALID"
