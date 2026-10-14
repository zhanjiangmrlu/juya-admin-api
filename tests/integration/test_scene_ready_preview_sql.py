"""READY sentence previews require current owned, complete immutable preparation facts."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.shared.errors import AppError
from tests.integration.test_v15_audio_proofreading_sql import database, prepared_cohort


async def observed_state(sessions):
    # Exact rows include statuses, hashes, snapshots, receipts and paid-attempt counts.
    async with sessions() as session:
        return {
            table: list((await session.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all())
            for table in (
                "scene",
                "scene_revision",
                "audio_version",
                "sentence_audio_slice",
                "media_asset",
                "content_import_stage",
                "audit_event",
                "proofreading_confirmation",
                "proofreading_set",
                "content_cloud_attempt",
                "cloud_usage_attempt",
            )
        }


async def resource_aliases(sessions, draft, version):
    async with sessions() as session:
        asset = await session.scalar(
            text("SELECT asset_id FROM sentence_audio_slice WHERE resource_version=:id"),
            {"id": version},
        )
    return [version, draft.content["dialogue"][0]["slice_target_id"], asset]


@pytest.mark.asyncio
async def test_ready_owned_slice_can_be_previewed_before_formal_confirmation():
    engine, sessions = database()
    try:
        _, store, _, cohort, drafts, audio_ids = await prepared_cohort(sessions)
        assert cohort["normal_count"] == 3
        draft = drafts[0]
        version = audio_ids[0][1]
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM audio_version WHERE public_id=:id"), {"id": version}
                )
                == "READY"
            )
            assert (
                await session.scalar(
                    text("SELECT status FROM sentence_audio_slice WHERE resource_version=:id"),
                    {"id": version},
                )
                == "READY"
            )
        aliases = await resource_aliases(sessions, draft, version)
        before = await observed_state(sessions)
        queries = []

        def record(_conn, _cursor, statement, _parameters, _context, _many):
            queries.append(statement.upper())

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            for alias in aliases:
                fact = await store.resource(draft.scene_id, draft.id, alias, published=False)
                assert fact["public_id"] == aliases[2]
                assert fact["object_key"].startswith("sealed/media/audio/")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert queries and all(query.lstrip().startswith("SELECT ") for query in queries)
        assert not any("FOR UPDATE" in query or "LOCK IN SHARE MODE" in query for query in queries)
        assert await observed_state(sessions) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "foreign",
        "stale",
        "source",
        "dependency",
        "english",
        "encoding",
        "duration",
        "size",
        "sha",
        "role",
        "blocked",
        "review_required",
        "failed",
        "v1",
    ],
)
async def test_ready_slice_preview_rejects_invalid_facts_without_mutation(change):
    engine, sessions = database()
    try:
        _, store, _, _, drafts, audio_ids = await prepared_cohort(sessions)
        draft = drafts[0]
        version = audio_ids[0][1]
        aliases = await resource_aliases(sessions, draft, version)
        snapshot = deepcopy(draft.content)
        async with sessions() as session, session.begin():
            if change == "review_required":
                store.require_review = True
            elif change in {"english", "v1"}:
                if change == "english":
                    snapshot["dialogue"][0]["english"] = "Changed source text."
                else:
                    snapshot["schema_version"] = 1
                await session.execute(
                    text(
                        "UPDATE scene_revision SET content_snapshot=:snapshot WHERE public_id=:id"
                    ),
                    {"id": draft.id, "snapshot": encode(snapshot)},
                )
            else:
                statements = {
                    "foreign": "UPDATE sentence_audio_slice SET content_id=:foreign "
                    "WHERE resource_version=:version",
                    "stale": "UPDATE scene SET draft_revision_id=NULL WHERE public_id=:scene",
                    "source": "UPDATE sentence_audio_slice SET source_sha256=:invalid_hash "
                    "WHERE resource_version=:version",
                    "dependency": "UPDATE sentence_audio_slice SET dependency_hash=:invalid_hash "
                    "WHERE resource_version=:version",
                    "encoding": "UPDATE sentence_audio_slice SET encoding_config='{}' "
                    "WHERE resource_version=:version",
                    "duration": "UPDATE media_asset SET duration_ms=9000 WHERE public_id=:asset",
                    "size": "UPDATE media_asset SET size_bytes=0 WHERE public_id=:asset",
                    "sha": "UPDATE media_asset SET sha256=:invalid_hash WHERE public_id=:asset",
                    "role": "UPDATE audio_target SET target_type='WHOLE' WHERE public_id=:target",
                    "blocked": "UPDATE media_asset SET security_status='BLOCKED' "
                    "WHERE public_id=:asset",
                    "failed": "UPDATE audio_version SET status='FAILED' WHERE public_id=:version",
                }
                await session.execute(
                    text(statements[change]),
                    {
                        "version": version,
                        "scene": draft.scene_id,
                        "asset": aliases[2],
                        "target": aliases[1],
                        "foreign": drafts[1].scene_id,
                        "invalid_hash": "0" * 64,
                    },
                )
        before = await observed_state(sessions)
        # v1 keeps its preexisting asset semantics; version and target READY stay denied.
        for alias in aliases[:2] if change == "v1" else aliases:
            with pytest.raises(AppError) as rejected:
                await store.resource(draft.scene_id, draft.id, alias, published=False)
            assert rejected.value.code == "RESOURCE_NOT_READY"
            assert rejected.value.status_code == 403
        with pytest.raises(AppError) as unrelated:
            await store.resource(draft.scene_id, draft.id, audio_ids[1][1], published=False)
        assert unrelated.value.code == "RESOURCE_NOT_REFERENCED"
        assert await observed_state(sessions) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_published_slice_preview_cannot_use_ready_version_target_or_asset_alias():
    engine, sessions = database()
    try:
        proof, store, actor, cohort, drafts, audio_ids = await prepared_cohort(sessions)
        candidate = next(
            c for c in cohort["candidates"] if c["revision_id"] == cohort["sample_ids"][0]
        )
        await proof.confirm(
            cohort["public_id"],
            candidate["revision_id"],
            candidate["candidate_hash"],
            actor,
            {"text_checked": True, "audio_listened": True},
        )
        await proof.bulk_confirm(
            cohort["public_id"],
            actor,
            cohort["collection_hash"],
            [d.id for d in drafts],
            True,
            uuid4().hex,
        )
        await proof.complete(cohort["public_id"], actor, allow_abnormal_drafts=True)
        draft = drafts[0]
        await store.publish(draft, actor, uuid4().hex, datetime.now(UTC))
        aliases = await resource_aliases(sessions, draft, audio_ids[0][1])
        for alias in aliases:
            assert (await store.resource(draft.scene_id, draft.id, alias, published=True))[
                "public_id"
            ] == aliases[2]
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE audio_version SET status='READY' WHERE public_id=:id"),
                {"id": audio_ids[0][1]},
            )
        before = await observed_state(sessions)
        for published in [True, False]:
            for alias in aliases:
                with pytest.raises(AppError) as rejected:
                    await store.resource(draft.scene_id, draft.id, alias, published=published)
                assert rejected.value.code == "RESOURCE_NOT_READY"
        assert await observed_state(sessions) == before
    finally:
        await engine.dispose()
