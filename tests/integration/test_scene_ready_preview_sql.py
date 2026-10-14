"""READY sentence previews require current owned, complete immutable preparation facts."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from juya_admin_api.modules.content.production_store import encode
from juya_admin_api.shared.errors import AppError
from tests.integration.test_v15_audio_proofreading_sql import database, prepared_cohort


def row_digest_sql(columns):
    # Keep SQL NULL distinct from JSON null, arbitrary bytes intact, and fields/type tags separate.
    fields = []
    for column, data_type in columns:
        name = "`" + column.replace("`", "``") + "`"
        tag = data_type.replace("'", "''")
        fields.append(f"JSON_ARRAY('{tag}',{name} IS NULL,HEX(CAST({name} AS BINARY)))")
    return f"SHA2(CAST(JSON_ARRAY({','.join(fields)}) AS CHAR CHARACTER SET utf8mb4),256)"


async def observed_state(sessions):
    # Hash every field of every row server-side; never return huge unrelated JSON packets.
    state = {}
    async with sessions() as session:
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
        ):
            columns = (
                await session.execute(
                    text(
                        "SELECT COLUMN_NAME,DATA_TYPE FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
                        "ORDER BY ORDINAL_POSITION"
                    ),
                    {"table": table},
                )
            ).all()
            assert columns
            digests = list(
                (
                    await session.scalars(text(f"SELECT {row_digest_sql(columns)} FROM `{table}`"))
                ).all()
            )
            assert all(isinstance(digest, str) and len(digest) == 64 for digest in digests)
            # Sorting the 64-character digests here avoids MySQL filesorting huge source rows.
            state[table] = sorted(digests)
    return state


@pytest.mark.asyncio
async def test_row_fingerprint_distinguishes_null_binary_types_and_field_boundaries():
    engine, sessions = database()
    try:
        async with sessions() as session:
            for left, right, columns in [
                ("NULL AS v", "CAST('null' AS JSON) AS v", [("v", "json")]),
                ("CAST('null' AS JSON) AS v", "JSON_QUOTE('null') AS v", [("v", "json")]),
                ("NULL AS v", "X'' AS v", [("v", "varbinary")]),
                ("X'00FF' AS v", "X'00FE' AS v", [("v", "varbinary")]),
                ("'a' AS a,'bc' AS b", "'ab' AS a,'c' AS b", [("a", "varchar"), ("b", "varchar")]),
                ("1.01 AS v", "1.02 AS v", [("v", "decimal")]),
            ]:
                digest = row_digest_sql(columns)
                first = await session.scalar(text(f"SELECT {digest} FROM (SELECT {left}) AS value"))
                second = await session.scalar(
                    text(f"SELECT {digest} FROM (SELECT {right}) AS value")
                )
                assert first != second
            integer = row_digest_sql([("v", "int")])
            string = row_digest_sql([("v", "varchar")])
            assert await session.scalar(
                text(f"SELECT {integer} FROM (SELECT 1 AS v) AS value")
            ) != (await session.scalar(text(f"SELECT {string} FROM (SELECT '1' AS v) AS value")))
    finally:
        await engine.dispose()


async def resource_aliases(sessions, draft, version):
    async with sessions() as session:
        asset = await session.scalar(
            text("SELECT asset_id FROM sentence_audio_slice WHERE resource_version=:id"),
            {"id": version},
        )
    return [version, draft.content["dialogue"][0]["slice_target_id"], asset]


@pytest.mark.asyncio
async def test_observed_state_retains_large_foreign_scope_and_tail_mutations():
    engine, sessions = database()
    identity = "preview-evidence-" + uuid4().hex
    try:
        _, store, _, _, drafts, audio_ids = await prepared_cohort(sessions)
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO proofreading_set "
                    "(public_id,collection_hash,candidates,normal_count,sample_ids,created_by) "
                    "VALUES(:id,:hash,JSON_ARRAY(JSON_OBJECT('content_type','dialogue',"
                    "'content_id',:id,'revision_id',:id,'text',REPEAT('大',700000))),"
                    "1,JSON_ARRAY(:id),'unrelated-evidence-fixture')"
                ),
                {"id": identity, "hash": "a" * 64},
            )
            size = await session.scalar(
                text("SELECT OCTET_LENGTH(candidates) FROM proofreading_set WHERE public_id=:id"),
                {"id": identity},
            )
            assert size > 2_000_000
        before = await observed_state(sessions)
        assert len(before) == 11
        fact = await store.resource(
            drafts[0].scene_id, drafts[0].id, audio_ids[0][1], published=False
        )
        assert fact["object_key"].startswith("sealed/media/audio/")
        assert await observed_state(sessions) == before
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE proofreading_set SET candidates=JSON_SET(candidates,'$[0].text',"
                    "CONCAT(REPEAT('大',699999),'尾')) WHERE public_id=:id"
                ),
                {"id": identity},
            )
        after_tail = await observed_state(sessions)
        assert after_tail["proofreading_set"] != before["proofreading_set"]
        assert all(after_tail[t] == rows for t, rows in before.items() if t != "proofreading_set")
        async with sessions() as session, session.begin():
            await session.execute(
                text("UPDATE proofreading_set SET status='COMPLETED' WHERE public_id=:id"),
                {"id": identity},
            )
        after_status = await observed_state(sessions)
        assert after_status["proofreading_set"] != after_tail["proofreading_set"]
        async with sessions() as session, session.begin():
            await session.execute(
                text("DELETE FROM proofreading_set WHERE public_id=:id"), {"id": identity}
            )
        after_delete = await observed_state(sessions)
        assert len(after_delete["proofreading_set"]) == len(before["proofreading_set"]) - 1
    finally:
        await engine.dispose()


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
