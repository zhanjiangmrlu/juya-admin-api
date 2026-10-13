"""Uploaded meanings remain local; central alternatives require explicit, audited selection."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.schemas import SceneEntry
from juya_admin_api.shared.errors import AppError
from tests.integration.test_content_work_orders import db as work_order_db

db = work_order_db


@pytest.mark.asyncio
async def test_supplied_meaning_survives_organize_and_central_multiple_senses(db):
    central = await db.production.write_entry(
        SceneEntry(english="bank", chinese="银行", variants=["banks"]), "VOCABULARY", db.actor
    )
    scene = await db.production.create_scene(db.series, "word_card")
    from juya_admin_api.shared.ids import new_ulid

    revision = new_ulid(datetime.now(UTC))
    saved = await db.production.save_revision(
        SceneRevision(
            revision,
            scene,
            None,
            content={
                "schema_version": 2,
                "template_type": "word_card",
                "title_en": "Bank",
                "title_zh": "河岸",
                "copyright": "Owned",
                "source": "Test",
                "vocabulary": [
                    dict(
                        english="bank",
                        chinese="河岸",
                        source_sentence="a bank",
                        source_locator={"start": "2", "end": "6"},
                    )
                ],
            },
        ),
        None,
    )
    order = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=scene,
        content=None,
        actor=db.actor,
        key=uuid4().hex,
    )
    item = (await db.service.items(order.work_order_id)).items[0]
    organized = await db.service.command(
        order.work_order_id,
        "organize",
        dict(expected_version=order.version, scope="all", selected_item_ids=[]),
        db.actor,
        uuid4().hex,
    )
    groups = await db.service.candidate_groups(order.work_order_id)
    group = groups.groups[0]
    result = group.results[0]
    assert result.provided.chinese == "河岸"
    assert result.central_options[0].content.chinese == "银行"
    assert result.central_options[0].lexicon_entry_id == central.entry_id
    assert result.entry_id == saved.content["vocabulary"][0]["entry_id"]
    receipt = await db.service.command(
        order.work_order_id,
        "candidate-decisions",
        dict(
            expected_version=organized.version,
            scope="selected",
            selected_item_ids=[item.item_id],
            candidate_id=group.candidate_id,
            expected_edit_version=saved.version,
            decisions=[
                dict(
                    result_id=result.result_id, decision="KEEP_PROVIDED", central_action="new_sense"
                )
            ],
        ),
        db.actor,
        uuid4().hex,
    )
    assert receipt.status == "SUCCEEDED"
    after = await db.service.item(order.work_order_id, item.item_id)
    assert after.current.content.vocabulary[0].chinese == "河岸"
    assert after.current.content.vocabulary[0].entry_id == result.entry_id
    async with db.sessions() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT v.content FROM lexicon_active_sense a JOIN "
                        "lexicon_entry_version v ON v.entry_id=a.entry_id AND v.version=a.version "
                        "WHERE a.entry_id=:id"
                    ),
                    {"id": central.entry_id},
                )
            )
            .scalars()
            .all()
        )
        import json

        assert {json.loads(r)["chinese"] if isinstance(r, str) else r["chinese"] for r in rows} == {
            "银行",
            "河岸",
        }
        link = (
            await session.execute(
                text(
                    "SELECT lexicon_entry_id,lexicon_entry_version FROM "
                    "content_entry WHERE public_id=:id"
                ),
                {"id": result.entry_id},
            )
        ).first()
        assert link.lexicon_entry_id == central.entry_id and link.lexicon_entry_version == 2
    edited = after.current.content.model_copy(deep=True)
    edited.vocabulary[0].chinese = "岸边"
    edited.vocabulary[0].meaning_change = "wording"
    await db.production.save_revision(
        SceneRevision(saved.id, scene, None, content=edited.model_dump()), after.edit_version
    )
    async with db.sessions() as session:
        assert (
            await session.scalar(
                text("SELECT lexicon_entry_id FROM content_entry WHERE public_id=:id"),
                {"id": result.entry_id},
            )
            is None
        )


@pytest.mark.asyncio
async def test_active_senses_wording_replaces_only_selected_sense_and_rejects_stale(db):
    from juya_admin_api.modules.content_production.lexicon import active_options, add_definition

    central = await db.production.write_entry(
        SceneEntry(english="riverbank-test", chinese="银行", variants=["riverbank-tests"]),
        "VOCABULARY",
        db.actor,
    )
    async with db.sessions() as session, session.begin():
        options = await active_options(session, SceneEntry(english="riverbank-tests"), "VOCABULARY")
        assert len(options) == 1 and options[0]["match"] == "variant"
        identifier, version = await add_definition(
            session,
            SceneEntry(english="riverbank-test", chinese="河岸"),
            "VOCABULARY",
            db.actor,
            "new_sense",
            options[0],
        )
        assert identifier == central.entry_id and version == 2
        await add_definition(
            session,
            SceneEntry(english="riverbank-test", chinese="银行机构", variants=["riverbank-tests"]),
            "VOCABULARY",
            db.actor,
            "wording",
            options[0],
        )
    async with db.sessions() as session, session.begin():
        options_now = await active_options(
            session, SceneEntry(english="riverbank-test"), "VOCABULARY"
        )
        assert {o["content"]["chinese"] for o in options_now} == {"河岸", "银行机构"}
        with pytest.raises(AppError) as denied:
            await add_definition(
                session,
                SceneEntry(english="riverbank-test", chinese="错误更新"),
                "VOCABULARY",
                db.actor,
                "wording",
                options[0],
            )
        assert denied.value.code == "CENTRAL_DEFINITION_STALE"


@pytest.mark.asyncio
async def test_reorganize_refreshes_expired_options_without_reopening_decided_entries(db):
    from juya_admin_api.modules.content_production.candidates import decide_provided
    from juya_admin_api.modules.content_production.lexicon import add_definition
    from juya_admin_api.shared.ids import new_ulid

    await db.production.write_entry(
        SceneEntry(english="refresh-bank", chinese="银行"), "VOCABULARY", db.actor
    )
    scene = await db.production.create_scene(db.series, "word_card")
    saved = await db.production.save_revision(
        SceneRevision(
            new_ulid(datetime.now(UTC)),
            scene,
            None,
            content=dict(
                schema_version=2,
                template_type="word_card",
                vocabulary=[
                    dict(english="untouched-entry", chinese="保持原文"),
                    dict(english="refresh-bank", chinese="上传释义"),
                ],
            ),
        ),
        None,
    )
    order = await db.service.manual(
        series_id=db.series,
        content_type="word_card",
        content_id=scene,
        content=None,
        actor=db.actor,
        key="refresh-order",
    )
    original = (await db.service.candidate_groups(order.work_order_id)).groups[0]
    first, second = original.results
    await decide_provided(
        db.service,
        original.candidate_id,
        [dict(result_id=first.result_id, decision="KEEP_PROVIDED")],
        saved.version,
        db.actor,
        "first-decided",
    )
    async with db.sessions() as session, session.begin():
        await add_definition(
            session,
            SceneEntry(english="refresh-bank", chinese="银行机构"),
            "VOCABULARY",
            db.actor,
            "wording",
            second.central_options[0].model_dump(),
        )
    await db.service.command(
        order.work_order_id,
        "organize",
        dict(expected_version=order.version, scope="all", selected_item_ids=[]),
        db.actor,
        "refresh",
    )
    refreshed = (await db.service.candidate_groups(order.work_order_id)).groups[0]
    assert refreshed.candidate_id != original.candidate_id
    assert [r.status for r in refreshed.results] == ["ADOPTED", "PENDING"]
    assert [r.entry_id for r in refreshed.results] == [first.entry_id, second.entry_id]
    assert refreshed.results[1].central_options[0].content.chinese == "银行机构"
    with pytest.raises(AppError) as stale:
        await decide_provided(
            db.service,
            original.candidate_id,
            [dict(result_id=second.result_id, decision="KEEP_PROVIDED")],
            saved.version,
            db.actor,
            "old-group",
        )
    assert stale.value.code == "CANDIDATE_SUPERSEDED"
    with pytest.raises(AppError) as decided:
        await decide_provided(
            db.service,
            refreshed.candidate_id,
            [dict(result_id=refreshed.results[0].result_id, decision="KEEP_PROVIDED")],
            saved.version,
            db.actor,
            "duplicate-first",
        )
    assert decided.value.code == "CANDIDATE_ALREADY_DECIDED"
    option = refreshed.results[1].central_options[0]
    await decide_provided(
        db.service,
        refreshed.candidate_id,
        [
            dict(
                result_id=refreshed.results[1].result_id,
                decision="USE_CENTRAL",
                meaning_change="new_sense",
                lexicon_entry_id=option.lexicon_entry_id,
                definition_version=option.definition_version,
            )
        ],
        saved.version,
        db.actor,
        "new-option",
    )
    item = (await db.service.items(order.work_order_id)).items[0]
    detail = await db.service.item(order.work_order_id, item.item_id)
    assert detail.current.content.vocabulary[1].chinese == "银行机构"
    assert detail.current.content.vocabulary[1].entry_id != second.entry_id
