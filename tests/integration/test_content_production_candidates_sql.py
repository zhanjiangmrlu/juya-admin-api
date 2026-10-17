"""Per-result decisions use actual SQL lineage, versions and per-content identities."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from juya_admin_api.integrations.content_ai.aliyun import ContentAIResult, TextCandidate
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.v15_candidates import CandidateService
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_work_orders import db as work_order_db

db = work_order_db


class Suggestions:
    model = "deterministic-test"

    async def suggest(self, sources, *, max_output_tokens):
        return ContentAIResult(
            tuple(
                TextCandidate("line", en, zh)
                for en, zh in [("bank", "河岸"), ("tree", "树"), ("house", "房子")]
            ),
            {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            "test",
        )


async def candidate(db, template="dialogue"):
    scene = await db.production.create_scene(db.series, template)
    from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
    from juya_admin_api.modules.content.service import ContentService

    content = ContentService(SQLAlchemyContentRepository(db.sessions))
    if template == "word_card":
        from juya_admin_api.shared.ids import new_ulid

        draft = SceneRevision(new_ulid(datetime.now(UTC)), scene, None)
        expected_version = None
    else:
        draft = await content.create_revision(scene, None, db.actor, datetime.now(UTC))
        expected_version = draft.version
    draft = await db.production.save_revision(
        SceneRevision(
            draft.id,
            scene,
            None,
            content={
                "schema_version": 2,
                "template_type": template,
                "dialogue": [
                    dict(id="line", speaker="A", english="bank tree house", chinese="河岸 树 房子")
                ],
            },
        ),
        expected_version,
    )
    service = CandidateService(
        db.sessions,
        db.production,
        StageStore(db.sessions),
        ai=Suggestions(),
        content_ai_daily_token_limit=100000,
    )
    identifier = await service.queue(
        draft.id, "AI_LEXICON", draft.version, db.actor, datetime.now(UTC)
    )
    await service.run(identifier, uuid4().hex, datetime.now(UTC))
    return service, identifier, draft


@pytest.mark.asyncio
async def test_word_card_with_bilingual_source_can_explicitly_organize_ai_recommendations(db):
    service, identifier, draft = await candidate(db, "word_card")
    db.service.candidates = service
    order = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=draft.scene_id,
        content=None,
        actor=db.actor,
        key=uuid4().hex,
    )
    result = await db.service.command(
        order.work_order_id,
        "organize",
        dict(
            expected_version=order.version,
            scope="all",
            selected_item_ids=[],
            include_recommendations=True,
        ),
        db.actor,
        "recommend-word-card",
    )
    assert result.stage_id == identifier
    groups = (await db.service.candidate_groups(order.work_order_id)).groups
    assert groups[0].kind == "AI_LEXICON" and len(groups[0].results) == 3
    item = (await db.service.items(order.work_order_id)).items[0]
    assert all(
        task.stage not in {"ASR_BOUNDARY", "ASR_POLL", "SENTENCE_SLICE"} for task in item.tasks
    )


@pytest.mark.asyncio
async def test_two_adoptions_from_same_result_set_and_rejection_are_independent(db):
    service, identifier, draft = await candidate(db)
    first = await service.adopt(
        identifier, 0, "line", 0, 4, "河岸", draft.version, db.actor, datetime.now(UTC)
    )
    second = await service.adopt(
        identifier, 1, "line", 5, 9, "树", first.version, db.actor, datetime.now(UTC)
    )
    assert [entry["english"] for entry in second.content["vocabulary"]] == ["bank", "tree"]
    current = await service.get(identifier)
    results = current["candidate"]["results"]
    assert [r["status"] for r in results] == ["ADOPTED", "ADOPTED", "PENDING"]
    assert len({r["result_id"] for r in results}) == 3
    receipt = await service.decide(
        identifier,
        [dict(result_id=results[2]["result_id"], decision="REJECTED")],
        second.version,
        db.actor,
        "reject",
    )
    assert (
        await service.decide(
            identifier,
            [dict(result_id=results[2]["result_id"], decision="REJECTED")],
            second.version,
            db.actor,
            "reject",
        )
        == receipt
    )
    assert (await service.get(identifier))["candidate"]["results"][2]["status"] == "REJECTED"
    assert len(receipt["content"]["vocabulary"]) == 2


@pytest.mark.asyncio
async def test_candidate_lineage_rejects_external_edit_even_after_own_adoption(db):
    service, identifier, draft = await candidate(db)
    first = await service.adopt(
        identifier, 0, "line", 0, 4, "河岸", draft.version, db.actor, datetime.now(UTC)
    )
    changed = await db.production.save_revision(
        SceneRevision(
            first.id, first.scene_id, None, content=first.content | {"title_en": "External edit"}
        ),
        first.version,
    )
    with pytest.raises(AppError) as denied:
        await service.adopt(
            identifier, 1, "line", 5, 9, "树", changed.version, db.actor, datetime.now(UTC)
        )
    assert denied.value.code == "CANDIDATE_INPUT_CHANGED"


@pytest.mark.asyncio
async def test_bulk_decision_replay_and_order_usage_count_actual_attempt_once(db):
    service, identifier, draft = await candidate(db)
    order = await db.service.manual(
        series_id=db.series,
        content_type="dialogue",
        content_id=draft.scene_id,
        content=None,
        actor=db.actor,
        key=uuid4().hex,
    )
    results = (await service.get(identifier))["candidate"]["results"]
    decisions = [
        dict(result_id=r["result_id"], decision="ADOPTED" if n < 2 else "REJECTED")
        for n, r in enumerate(results)
    ]
    receipt = await service.decide(identifier, decisions, draft.version, db.actor, "bulk")
    assert await service.decide(identifier, decisions, draft.version, db.actor, "bulk") == receipt
    assert len(receipt["content"]["vocabulary"]) == 2
    assert len({e["entry_id"] for e in receipt["content"]["vocabulary"]}) == 2
    usage = await db.service.usage(order.work_order_id)
    assert len(usage.attempts) == 1
    attempt = usage.attempts[0]
    assert (attempt.kind, attempt.actual_units, attempt.input_tokens, attempt.output_tokens) == (
        "AI_TOKENS",
        20,
        10,
        10,
    )
    assert attempt.model == "deterministic-test"
    assert usage.cost is None and usage.retry_count == 0


@pytest.mark.asyncio
async def test_pending_optional_results_require_explicit_rejection(db):
    from sqlalchemy import text

    from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
    from juya_admin_api.modules.content_production.effective_issues import current_issues_within

    service, identifier, draft = await candidate(db)
    proof = uuid4().hex
    hashed = snapshot_hash(draft.content)
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO proofreading_set(public_id,collection_hash,candidates,normal_count,"
                "sample_ids,status,created_by) VALUES(:id,:hash,'[]',1,'[]','CONFIRMED',:actor)"
            ),
            dict(id=proof, hash=hashed, actor=db.actor),
        )
        await session.execute(
            text(
                "INSERT INTO proofreading_confirmation(set_id,content_type,content_id,revision_id,"
                "candidate_hash,method,confirmed_by,confirmed_at) VALUES(:id,'dialogue',:content,"
                ":revision,:hash,'SAMPLE',:actor,UTC_TIMESTAMP(6))"
            ),
            dict(id=proof, content=draft.scene_id, revision=draft.id, hash=hashed, actor=db.actor),
        )
    async with db.sessions() as session, session.begin():
        pending = await current_issues_within(session, draft.scene_id, draft.id, draft.content)
        assert any(i["code"] == "LEXICON_CANDIDATE_PENDING" for i in pending)
        assert not await db.proof.is_confirmed_within(session, draft.id, hashed)
    results = (await service.get(identifier))["candidate"]["results"]
    await service.decide(
        identifier,
        [dict(result_id=r["result_id"], decision="REJECTED") for r in results],
        draft.version,
        db.actor,
        "skip-all",
    )
    async with db.sessions() as session, session.begin():
        assert not await current_issues_within(session, draft.scene_id, draft.id, draft.content)
        assert await db.proof.is_confirmed_within(session, draft.id, hashed)


@pytest.mark.asyncio
async def test_paid_enqueue_receipt_recovers_after_external_edit(db):
    service, identifier, draft = await candidate(db)
    queued = await service.queue(
        draft.id,
        "AI_LEXICON",
        draft.version,
        db.actor,
        datetime.now(UTC),
        idempotency_key="recover-enqueue",
    )
    assert queued == identifier
    await db.production.save_revision(
        SceneRevision(
            draft.id, draft.scene_id, None, content=draft.content | {"title_en": "Later"}
        ),
        draft.version,
    )
    assert (
        await service.queue(
            draft.id,
            "AI_LEXICON",
            draft.version,
            db.actor,
            datetime.now(UTC),
            idempotency_key="recover-enqueue",
        )
        == queued
    )
