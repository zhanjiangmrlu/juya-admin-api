"""Real isolated SQL acceptance of modern authoring, immutable meanings and typed access."""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore, decode
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
from juya_admin_api.modules.content.v15_contracts import ConfirmationCredential, FavoriteReference
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.modules.content_import.stages import StageStore
from juya_admin_api.modules.short_reading.store import ShortReadingStore
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.service import V15AccessService
from juya_admin_api.modules.v15_access.sql_adapters import (
    SQLAuthorizer,
    SQLContentLoader,
    SQLFavoriteAuthority,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


def connection() -> Any:
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated MySQL required; no shared database is used")
    return create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))


def word_card() -> dict[str, Any]:
    return dict(
        schema_version=2,
        template_type="word_card",
        title_en="Medium meanings",
        title_zh="两种意思",
        copyright="Owned isolated fixture",
        source="Fixture owner",
        vocabulary=[
            dict(english="medium", chinese="中杯", source_sentence="A medium coffee."),
            dict(english="medium", chinese="中等", source_sentence="This is medium difficulty."),
        ],
    )


async def saved_word_card(
    store: ProductionStore, service: ContentService, series: str, actor: str
) -> SceneRevision:
    identity = await store.create_scene(series, "vocabulary", actor=actor, key=uuid4().hex)
    scene = await service.get_scene(identity)
    assert scene.draft_revision_id
    return await service.save_revision(
        scene.draft_revision_id, word_card(), expected_version=1, actor_id=actor
    )


@pytest.mark.asyncio
async def test_modern_word_card_confirm_publish_typed_access_and_replay() -> None:
    engine = connection()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    proof = ProofreadingStore(sessions)
    store = ProductionStore(sessions, check_confirmation=proof.is_confirmed_within)
    service = ContentService(
        SQLAlchemyContentRepository(sessions, check_confirmation=proof.is_confirmed_within)
    )
    authoring = SceneAuthoringStore(sessions, proof, StageStore(sessions))
    actor, user, package = [new_ulid(datetime.now(UTC)) for _ in range(3)]
    now = datetime.now(UTC)
    original_enabled = 1
    try:
        series = await store.create_series("V15 independent meanings", uuid4().hex, None)
        draft = await saved_word_card(store, service, series["id"], actor)
        first, second = draft.content["vocabulary"]
        assert first["english"] == second["english"] == "medium"
        assert all(first[key] != second[key] for key in ("entry_id", "sense_id", "source_id"))
        with pytest.raises(AppError) as blocked:
            await store.publish(draft, actor, uuid4().hex, now)
        assert blocked.value.code == "PUBLISH_CHECK_FAILED"
        for entry in (first, second):
            receipt = await authoring.confirm_entry(
                draft.id, entry["entry_id"], draft.version, actor, True
            )
            draft = await service.get_revision(receipt["revision_id"])
        assert all(entry["confirmed"] for entry in draft.content["vocabulary"])
        assert draft.content["audio"] is None and not draft.content["dialogue"]
        candidate_hash = snapshot_hash(draft.content)
        cohort = await proof.create(
            [
                dict(
                    revision_id=draft.id,
                    candidate_hash=candidate_hash,
                    content_type="word_card",
                    content_id=draft.scene_id,
                )
            ],
            actor,
        )
        with pytest.raises(AppError) as incomplete:
            await proof.complete(cohort["public_id"], actor)
        assert incomplete.value.code == "PROOFREADING_INCOMPLETE"
        await proof.confirm(
            cohort["public_id"], draft.id, candidate_hash, actor, {"text_checked": True}
        )
        # A single checked candidate is still not a completed fixed review collection.
        with pytest.raises(AppError) as unfinished:
            await store.publish(draft, actor, uuid4().hex, now)
        assert unfinished.value.code == "PUBLISH_CHECK_FAILED"
        await proof.complete(cohort["public_id"], actor)
        publish_key = uuid4().hex
        published = await service.publish_revision(
            draft.id, actor, publish_key, now, expected_version=draft.version
        )
        async with sessions() as session, session.begin():
            original_enabled = int(
                await session.scalar(
                    text(
                        "SELECT enabled FROM learning_module_config "
                        "WHERE module_type='scene_learning' FOR UPDATE"
                    )
                )
                or 0
            )
            await session.execute(
                text(
                    "UPDATE learning_module_config SET enabled=1 WHERE module_type='scene_learning'"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO user_account(public_id,juya_number,status) "
                    "VALUES(:id,:number,'ACTIVE')"
                ),
                {"id": user, "number": "TEST" + uuid4().hex[:14]},
            )
            await session.execute(
                text(
                    "INSERT INTO content_package(public_id,name,status) "
                    "VALUES(:id,'V15 typed fixture','ACTIVE')"
                ),
                {"id": package},
            )
            await session.execute(
                text(
                    "INSERT INTO content_package_item(package_id,content_type,"
                    "content_id,position) SELECT id,'word_card',:content,1 FROM content_package "
                    "WHERE public_id=:package"
                ),
                {"content": draft.scene_id, "package": package},
            )
            await session.execute(
                text(
                    "INSERT INTO formal_entitlement(public_id,user_id,package_id,"
                    "term,status,granted_at,expires_at,updated_at) "
                    "SELECT :id,u.id,p.id,'permanent','ACTIVE',:now,NULL,:now "
                    "FROM user_account u,content_package p "
                    "WHERE u.public_id=:user AND p.public_id=:package"
                ),
                {"id": new_ulid(datetime.now(UTC)), "now": now, "user": user, "package": package},
            )

        async def no_resource(
            session: Any, request: ResourceRequest, expires: datetime
        ) -> dict[str, Any]:
            raise AssertionError("pure word-card confirmation never signs an unrelated resource")

        access = V15AccessService(
            sessions,
            content_loader=SQLContentLoader(ShortReadingStore(sessions)),
            authorizer=SQLAuthorizer(),
            resource_signer=no_resource,
            credential_secret=b"private-isolated-confirmation-key",
            favorite_authority=SQLFavoriteAuthority(),
        )
        detail = await access.open(user, "word_card", draft.scene_id, now)
        assert detail["whole_audio"] is None and detail["dialogue"] == []
        assert [entry["meaning_zh"] for entry in detail["words"]] == ["中杯", "中等"]
        for entry in detail["words"]:
            ref = FavoriteReference(
                content_type="word_card",
                content_id=draft.scene_id,
                content_version=detail["content_version"],
                entry_id=entry["entry_id"],
                sense_id=entry["sense_id"],
                source_id=entry["source_id"],
            )
            result = await access.confirmation(user, ref, now)
            credential = ConfirmationCredential.model_validate(result["credential"])
            async with sessions() as session, session.begin():
                checked = await access.verify_confirmation_within(session, credential, user, now)
                assert checked.meaning_zh == entry["meaning_zh"]
            invalid = ref.model_copy(update={"source_id": detail["words"][1]["source_id"]})
            if invalid.source_id != ref.source_id:
                with pytest.raises(AppError):
                    await access.confirmation(user, invalid, now)
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE scene SET status='OFFLINE' WHERE public_id=:id"),
                {"id": draft.scene_id},
            )
            await session.execute(
                text(
                    "UPDATE learning_module_config SET enabled=0 WHERE module_type='scene_learning'"
                )
            )
        # Revoked review facts are irrelevant to an already committed same-key receipt.
        async with sessions() as session, session.begin():
            await proof.invalidate_within(session, draft.id)
        assert (
            await service.publish_revision(
                draft.id, actor, publish_key, now, expected_version=draft.version
            )
            == published
        )
        with pytest.raises(AppError):
            await access.open(user, "word_card", draft.scene_id, now)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE learning_module_config SET enabled=:enabled "
                    "WHERE module_type='scene_learning'"
                ),
                {"enabled": original_enabled},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_identity_cas_meaning_source_changes_do_not_rewrite_published_snapshot() -> None:
    engine = connection()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    proof = ProofreadingStore(sessions)
    store = ProductionStore(sessions, check_confirmation=proof.is_confirmed_within)
    service = ContentService(
        SQLAlchemyContentRepository(sessions, check_confirmation=proof.is_confirmed_within)
    )
    authoring = SceneAuthoringStore(sessions, proof, StageStore(sessions))
    actor = new_ulid(datetime.now(UTC))
    try:
        series = await store.create_series("V15 identity changes", uuid4().hex, None)
        draft = await saved_word_card(store, service, series["id"], actor)
        original = draft.content["vocabulary"][0].copy()
        other = draft.content["vocabulary"][1].copy()
        for entry in (original, other):
            await authoring.confirm_entry(draft.id, entry["entry_id"], draft.version, actor, True)
            draft = await service.get_revision(draft.id)
        candidate_hash = snapshot_hash(draft.content)
        cohort = await proof.create(
            [
                dict(
                    revision_id=draft.id,
                    candidate_hash=candidate_hash,
                    content_type="word_card",
                    content_id=draft.scene_id,
                )
            ],
            actor,
        )
        await proof.confirm(
            cohort["public_id"], draft.id, candidate_hash, actor, {"text_checked": True}
        )
        await proof.complete(cohort["public_id"], actor)
        await service.publish_revision(
            draft.id, actor, uuid4().hex, datetime.now(UTC), expected_version=draft.version
        )
        snapshot = (await store.full_scene(draft.scene_id))["content"]
        working = await service.create_revision(draft.scene_id, draft.id, actor, datetime.now(UTC))
        content = decode(working.content)
        entry = content["vocabulary"][0]
        entry["english"] = "Medium"
        entry["confirmed"] = True  # Client control fields cannot manufacture a new confirmation.
        entry["entry_version"] = 999
        working = await service.save_revision(
            working.id, content, expected_version=working.version, actor_id=actor
        )
        updated = working.content["vocabulary"][0]
        assert (updated["entry_id"], updated["sense_id"], updated["source_id"]) == (
            original["entry_id"],
            original["sense_id"],
            original["source_id"],
        )
        assert updated["entry_version"] < 999 and not updated["confirmed"]
        assert updated["pronunciation_target_id"] is None
        with pytest.raises(AppError) as stale:
            await service.save_revision(
                working.id, working.content, expected_version=working.version - 1, actor_id=actor
            )
        assert stale.value.code == "REVISION_VERSION_CONFLICT"
        changed = decode(working.content)
        changed["vocabulary"][0]["chinese"] = "中等的"
        with pytest.raises(AppError) as unspecified:
            await service.save_revision(
                working.id, changed, expected_version=working.version, actor_id=actor
            )
        assert unspecified.value.code == "MEANING_CHANGE_CONFIRMATION_REQUIRED"
        changed["vocabulary"][0]["meaning_change"] = "new_sense"
        changed["vocabulary"][0]["source_sentence"] = "A medium level of difficulty."
        changed["vocabulary"][0]["source_locator"] = {"paragraph_id": "new-source"}
        working = await service.save_revision(
            working.id, changed, expected_version=working.version, actor_id=actor
        )
        new = working.content["vocabulary"][0]
        assert new["entry_id"] != original["entry_id"]
        assert new["sense_id"] != original["sense_id"] and new["source_id"] != original["source_id"]
        assert new["entry_id"] != other["entry_id"] and new["sense_id"] != other["sense_id"]
        assert (await store.full_scene(draft.scene_id))["content"] == snapshot
        second_parent = await saved_word_card(store, service, series["id"], actor)
        cross_parent = decode(second_parent.content)
        cross_parent["vocabulary"][0] = dict(new)
        with pytest.raises(AppError) as ownership:
            await service.save_revision(
                second_parent.id,
                cross_parent,
                expected_version=second_parent.version,
                actor_id=actor,
            )
        assert ownership.value.code == "ENTRY_IDENTITY_CONFLICT"
        forged = decode(working.content)
        forged["vocabulary"][0]["sense_id"] = "forged-sense"
        with pytest.raises(AppError) as sense:
            await service.save_revision(
                working.id, forged, expected_version=working.version, actor_id=actor
            )
        assert sense.value.code == "SENSE_IDENTITY_CONFLICT"
        # A rollback copies an explicit historical published revision, not the newest draft meaning.
        rollback = await service.create_revision(draft.scene_id, draft.id, actor, datetime.now(UTC))
        assert rollback.content == snapshot
        assert rollback.content["vocabulary"][0]["sense_id"] == original["sense_id"]
        assert rollback.content["vocabulary"][0]["source_id"] == original["source_id"]
    finally:
        await engine.dispose()
