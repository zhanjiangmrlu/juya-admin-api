"""Real publishers/SQL proof approvals and crash recovery, without provider calls."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import test_v15_worker_dispatch as dispatch
from sqlalchemy import text

from juya_admin_api.modules.content.publication_adapter import build_publication_service
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_proofreading import snapshot_hash
from juya_admin_api.modules.short_reading.store import digest
from juya_admin_api.shared.errors import AppError

dispatched_runtime = dispatch.dispatched_runtime


async def ready_candidate(db, kind):
    services = db.services
    content = ContentService(
        SQLAlchemyContentRepository(
            db.sessions,
            check_confirmation=services.proofreading.is_confirmed_within,
        )
    )
    if kind == "short_reading":
        created = await services.readings.command(
            "create",
            "new",
            db.actor,
            uuid4().hex,
            {
                "series_id": db.series,
                "content": {
                    "title_en": "Read",
                    "title_zh": "阅读",
                    "category": "Daily",
                    "topic": "Life",
                    "level": "A1",
                    "safe_intro": "A day",
                    "paragraphs_en": ["A full English paragraph."],
                    "translation_zh": "完整中文译文",
                    "copyright_status": "CONFIRMED",
                    "copyright_source": "Owned synthetic fixture",
                },
            },
        )
        async with db.sessions() as session:
            revision = await services.readings.revision(
                session, created["content_id"], created["revision_id"]
            )
        candidate = dict(
            content_type=kind,
            content_id=created["content_id"],
            revision_id=created["revision_id"],
            expected_version=1,
            expected_edit_version=revision["edit_version"],
            candidate_hash=digest(revision["content_snapshot"]),
            warning_codes=[],
        )
    else:
        scene = await services.production.create_scene(
            db.series, "word_card", actor=db.actor, key=uuid4().hex
        )
        async with db.sessions() as session:
            revision_id = await session.scalar(
                text(
                    "SELECT r.public_id FROM scene s JOIN scene_revision r "
                    "ON r.id=s.draft_revision_id WHERE s.public_id=:id"
                ),
                {"id": scene},
            )
        revision = await content.get_revision(revision_id)
        revision = await content.save_revision(
            revision_id,
            {
                "schema_version": 2,
                "template_type": "word_card",
                "title_en": "Bank",
                "title_zh": "银行",
                "copyright": "Owned synthetic fixture",
                "source": "Fixture",
                "vocabulary": [{"english": "bank", "chinese": "银行"}],
            },
            expected_version=revision.version,
            actor_id=db.actor,
        )
        await services.authoring.confirm_entry(
            revision.id,
            revision.content["vocabulary"][0]["entry_id"],
            revision.version,
            db.actor,
            True,
        )
        revision = await content.get_revision(revision.id)
        candidate = dict(
            content_type=kind,
            content_id=scene,
            revision_id=revision.id,
            expected_version=revision.version,
            candidate_hash=snapshot_hash(revision.content),
            warning_codes=[],
        )
    cohort = await services.proofreading.create(
        [
            {
                name: candidate[name]
                for name in ("content_type", "content_id", "revision_id", "candidate_hash")
            }
        ],
        db.actor,
    )
    await services.proofreading.confirm(
        cohort["public_id"],
        candidate["revision_id"],
        candidate["candidate_hash"],
        db.actor,
        {"text_checked": True},
    )
    await services.proofreading.complete(cohort["public_id"], db.actor)
    service = build_publication_service(
        db.sessions, services.imports.stages, services.production, content, services.readings
    )
    facts = await service.inspect_candidate(candidate)
    assert facts["ready"], facts
    return service, candidate, content


async def queue(service, candidate, actor):
    preview = await service.preview([candidate], actor)
    receipt = await service.confirm(
        preview["token"],
        actor,
        uuid4().hex,
        manual_confirmed=True,
        expected_version=preview["version"],
        collection_hash=preview["collection_hash"],
        selected_revision_ids=[candidate["revision_id"]],
    )
    return preview, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["word_card", "short_reading"])
async def test_real_range_publish_unknown_replays_committed_receipt(dispatched_runtime, kind):
    db = dispatched_runtime
    service, candidate, _content = await ready_candidate(db, kind)
    preview, receipt = await queue(service, candidate, db.actor)
    actual = service.publish_candidate
    calls = []
    committed = []
    failures = []

    async def disconnected(item, actor, key):
        calls.append(key)
        try:
            committed.append(await actual(item, actor, key))
        except Exception as error:
            failures.append(repr(error))
            raise
        raise ConnectionError("publisher committed; caller lost its response")

    service.publish_candidate = disconnected
    first = await service.run(receipt["stage_id"], "first-worker")
    assert committed, failures
    assert first["results"]["items"][candidate["revision_id"]]["status"] == "UNKNOWN"
    service.publish_candidate = actual
    # The committed publication is no longer the editable current draft. Recovery must read
    # its original business receipt before applying draft/readiness checks.
    assert not (await service.inspect_candidate(candidate))["ready"]
    retry = await service.confirm(
        preview["token"],
        db.actor,
        uuid4().hex,
        manual_confirmed=True,
        expected_version=first["version"],
        collection_hash=preview["collection_hash"],
        selected_revision_ids=[candidate["revision_id"]],
    )
    completed = await service.run(retry["stage_id"], "recovery-worker")
    assert completed["status"] == "SUCCEEDED"
    replay = await actual(candidate, db.actor, calls[0])
    assert replay["revision_id"] == candidate["revision_id"]
    async with db.sessions() as session:
        if kind == "short_reading":
            reading = await db.services.readings.row(session, candidate["content_id"])
            assert (
                reading["published_revision_id"] == candidate["revision_id"]
                and reading["version"] == 2
            )
        else:
            published = await _content.get_scene(candidate["content_id"])
            assert published.published_revision_id == candidate["revision_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["word_card", "short_reading"])
async def test_final_publisher_hash_guard_and_current_draft(dispatched_runtime, kind):
    db = dispatched_runtime
    service, candidate, content = await ready_candidate(db, kind)
    _preview, queued = await queue(service, candidate, db.actor)
    # Inject a concurrent snapshot change without relying on a version bump. Final SQL hash
    # comparison must catch it, even when earlier inspect/validation observed the old bytes.
    table = "short_reading_revision" if kind == "short_reading" else "scene_revision"
    async with db.sessions() as session, session.begin():
        await session.execute(
            text(
                f"UPDATE {table} SET content_snapshot=JSON_SET(content_snapshot,"
                "'$.title_en','Changed after preview') WHERE public_id=:id"
            ),
            {"id": candidate["revision_id"]},
        )
    stale = await service.run(queued["stage_id"], "after-edit-worker")
    assert stale["status"] == "PARTIAL"
    assert stale["results"]["items"][candidate["revision_id"]]["status"] == "FAILED"
    if kind == "short_reading":
        with pytest.raises(AppError) as changed:
            await service.publish_candidate(candidate, db.actor, uuid4().hex)
    else:
        revision = await content.get_revision(candidate["revision_id"])
        with pytest.raises(AppError) as changed:
            await db.services.production.publish(
                revision,
                db.actor,
                uuid4().hex,
                datetime.now(UTC),
                expected_candidate_hash=candidate["candidate_hash"],
            )
    assert changed.value.code == "PUBLICATION_PREVIEW_STALE"
    async with db.sessions() as session:
        status = await session.scalar(
            text(f"SELECT status FROM {table} WHERE public_id=:id"),
            {"id": candidate["revision_id"]},
        )
    assert status == "DRAFT"

    if kind == "short_reading":
        await db.services.readings.command(
            "revision",
            candidate["content_id"],
            db.actor,
            uuid4().hex,
            {"expected_version": 1, "source_revision_id": candidate["revision_id"]},
        )
        async with db.sessions() as session:
            old = await db.services.readings.revision(
                session, candidate["content_id"], candidate["revision_id"]
            )
        with pytest.raises(AppError) as obsolete:
            await db.services.readings.command(
                "publish",
                candidate["content_id"],
                db.actor,
                uuid4().hex,
                {
                    "expected_version": 2,
                    "revision_id": candidate["revision_id"],
                    "manual_confirmed": True,
                    "expected_edit_version": old["edit_version"],
                    "candidate_hash": digest(old["content_snapshot"]),
                },
            )
        assert obsolete.value.code == "REVISION_NOT_CURRENT"
