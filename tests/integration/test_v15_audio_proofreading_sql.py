"""Isolated SQL and actual FFmpeg bytes: sample listening approves only a frozen normal cohort."""

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.domain import SceneRevision
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.v15_proofreading import ProofreadingStore, snapshot_hash
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid
from tests.integration.test_v15_audio_authoring_flow import generated_wav


async def prepared_cohort(sessions, *, abnormal=False):
    # sessions 为隔离数据库工厂,abnormal 决定额外创建一个缺译文的异常候选
    proof = ProofreadingStore(sessions, require_review=False)
    production = ProductionStore(
        sessions, require_review=False, check_confirmation=proof.is_confirmed_within
    )
    actor = new_ulid(datetime.now(UTC))
    series = await production.create_series("Prepared audio review", uuid4().hex, None)
    source = generated_wav(440 + int(uuid4().hex[:4], 16))
    source_sha = hashlib.sha256(source).hexdigest()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "real FFmpeg is required for prepared review SQL tests"
    sliced_bytes = FFmpegSlicer(ffmpeg, ffprobe).slice(source, source_sha, Boundary(0, 1000))
    source_asset, slice_asset = [new_ulid(datetime.now(UTC)) for _ in range(2)]
    async with sessions() as session, session.begin():
        for identity, sha, size, duration in [
            (source_asset, source_sha, len(source), 3000),
            (slice_asset, sliced_bytes.sha256, len(sliced_bytes.data), sliced_bytes.duration_ms),
        ]:
            await session.execute(
                text(
                    "INSERT INTO "
                    "media_asset(public_id,object_key,asset_type,content_type,siz"
                    "e_bytes,sha256,status,security_status,created_by,created_at,"
                    "duration_ms) "
                    "VALUES(:id,:key,'audio','audio/wav',:size,:sha,'CONFIRMED','"
                    "SKIPPED',:actor,UTC_TIMESTAMP(6),:duration)"
                ),
                dict(
                    id=identity,
                    key="sealed/media/audio/" + sha,
                    size=size,
                    sha=sha,
                    actor=actor,
                    duration=duration,
                ),
            )
    candidates, drafts, audio_ids = [], [], []
    for index in range(4 if abnormal else 3):
        content_id = await production.create_scene(
            series["id"], "dialogue", actor=actor, key=uuid4().hex
        )
        whole_target, whole_version, slice_target, slice_version, slice_id = [
            new_ulid(datetime.now(UTC)) for _ in range(5)
        ]
        async with sessions() as session, session.begin():
            revision_id = await session.scalar(
                text(
                    "SELECT r.public_id FROM scene_revision r JOIN scene c ON "
                    "c.draft_revision_id=r.id WHERE c.public_id=:id"
                ),
                {"id": content_id},
            )
            for target, version, asset, kind in [
                (whole_target, whole_version, source_asset, "WHOLE"),
                (slice_target, slice_version, slice_asset, "sentence"),
            ]:
                await session.execute(
                    text(
                        "INSERT INTO audio_target(public_id,stable_key,target_type) "
                        "VALUES(:id,:key,:kind)"
                    ),
                    dict(id=target, key="proof-audio:" + uuid4().hex, kind=kind),
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "audio_version(public_id,target_id,asset_id,version_no,source"
                        ",status,created_by,created_at) SELECT "
                        ":version,t.id,m.id,1,'MANUAL','READY',:actor,UTC_TIMESTAMP(6"
                        ") FROM audio_target t,media_asset m WHERE "
                        "t.public_id=:target AND m.public_id=:asset"
                    ),
                    dict(version=version, target=target, asset=asset, actor=actor),
                )
        digest = dependency_hash(
            sentence_id="sentence",
            english="Hello.",
            source_sha256=source_sha,
            source_audio_version=whole_version,
            boundary=Boundary(0, 1000),
            encoding_config=FFmpegSlicer.encoding_config,
        )
        saved = await production.save_revision(
            SceneRevision(
                revision_id,
                content_id,
                None,
                content=dict(
                    schema_version=2,
                    template_type="dialogue",
                    title_en="Hello",
                    title_zh="你好",
                    copyright="Owned tone fixture",
                    source="Isolated test",
                    audio=dict(
                        target_id=whole_target,
                        version_id=whole_version,
                        asset_id=source_asset,
                        duration_ms=3000,
                    ),
                    dialogue=[
                        dict(
                            id="sentence",
                            speaker="A",
                            english="Hello.",
                            chinese="" if abnormal and index == 3 else "你好",
                            start_ms=0,
                            end_ms=1000,
                            audio_version_id=whole_version,
                            timing_confirmed=True,
                            boundary_verified_by="system:asr-alignment",
                            slice_target_id=slice_target,
                            slice_version=slice_version,
                            slice_dependency_hash=digest,
                        )
                    ],
                ),
            ),
            1,
        )
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO "
                    "sentence_audio_slice(public_id,content_type,content_id,revis"
                    "ion_id,sentence_id,source_audio_version,source_sha256,start_"
                    "ms,end_ms,target_id,resource_version,asset_id,dependency_has"
                    "h,status,actual_duration_ms,slice_sha256,byte_size,encoding_"
                    "config,created_at) "
                    "VALUES(:id,'dialogue',:content,:revision,'sentence',:source,"
                    ":sha,0,1000,:target,:version,:asset,:digest,'READY',:duratio"
                    "n,:slice_sha,:size,:encoding,UTC_TIMESTAMP(6))"
                ),
                dict(
                    id=slice_id,
                    content=content_id,
                    revision=revision_id,
                    source=whole_version,
                    sha=source_sha,
                    target=slice_target,
                    version=slice_version,
                    asset=slice_asset,
                    digest=digest,
                    duration=sliced_bytes.duration_ms,
                    slice_sha=sliced_bytes.sha256,
                    size=len(sliced_bytes.data),
                    encoding=json.dumps(FFmpegSlicer.encoding_config),
                ),
            )
        candidates.append(
            dict(
                content_type="dialogue",
                content_id=content_id,
                revision_id=revision_id,
                candidate_hash=snapshot_hash(saved.content),
            )
        )
        drafts.append(saved)
        audio_ids.append((whole_version, slice_version, slice_id))
    cohort = await proof.create(candidates, actor)
    return proof, production, actor, cohort, drafts, audio_ids


def database():
    # 返回仅显式提供的隔离数据库连接,不读取业务 env 文件
    url = os.getenv("JUYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated migrated MySQL required")
    engine = create_async_engine(url.replace("mysql+pymysql://", "mysql+asyncmy://"))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_sample_then_batch_approves_ready_audio_without_snapshot_mutation():
    engine, sessions = database()
    try:
        proof, production, actor, cohort, drafts, audio_ids = await prepared_cohort(
            sessions, abnormal=True
        )
        assert cohort["normal_count"] == 3 and len(cohort["sample_ids"]) == 1
        candidates = {item["revision_id"]: item for item in cohort["candidates"]}
        normals = [identity for identity, item in candidates.items() if item["normal"]]
        sample = candidates[cohort["sample_ids"][0]]
        with pytest.raises(AppError) as denied:
            await proof.confirm(
                cohort["public_id"],
                sample["revision_id"],
                sample["candidate_hash"],
                actor,
                {"text_checked": True},
            )
        assert denied.value.code == "PROOFREADING_CHECK_REQUIRED"
        with pytest.raises(AppError):
            await proof.bulk_confirm(
                cohort["public_id"], actor, cohort["collection_hash"], normals, True
            )
        await proof.confirm(
            cohort["public_id"],
            sample["revision_id"],
            sample["candidate_hash"],
            actor,
            {"text_checked": True, "audio_listened": True},
        )
        async with sessions() as session:
            assert (
                await session.scalar(
                    text("SELECT status FROM sentence_audio_slice WHERE revision_id=:id"),
                    {"id": sample["revision_id"]},
                )
                == "CONFIRMED"
            )
        key = uuid4().hex
        receipt = await proof.bulk_confirm(
            cohort["public_id"], actor, cohort["collection_hash"], normals, True, key
        )
        await proof.complete(cohort["public_id"], actor, allow_abnormal_drafts=True)
        assert (
            await proof.bulk_confirm(
                cohort["public_id"], actor, cohort["collection_hash"], normals, True, key
            )
            == receipt
        )
        async with sessions() as session:
            for draft, ids in zip(drafts, audio_ids, strict=True):
                snapshot = await session.scalar(
                    text("SELECT content_snapshot FROM scene_revision WHERE public_id=:id"),
                    {"id": draft.id},
                )
                assert snapshot_hash(
                    json.loads(snapshot) if isinstance(snapshot, str) else snapshot
                ) == snapshot_hash(draft.content)
                statuses = (
                    (
                        await session.execute(
                            text(
                                "SELECT status FROM audio_version "
                                "WHERE public_id IN (:whole,:slice)"
                            ),
                            {"whole": ids[0], "slice": ids[1]},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert statuses == (
                    ["CONFIRMED", "CONFIRMED"] if draft.id in normals else ["READY", "READY"]
                )
                assert await proof.is_confirmed_within(
                    session, draft.id, snapshot_hash(draft.content)
                ) is (draft.id in normals)
            methods = (
                (
                    await session.execute(
                        text("SELECT method FROM proofreading_confirmation WHERE set_id=:id"),
                        {"id": cohort["public_id"]},
                    )
                )
                .scalars()
                .all()
            )
            assert methods.count("MANUAL") == 1 and methods.count("BATCH_NORMAL") == 2
            audits = (
                (
                    await session.execute(
                        text(
                            (
                                "SELECT after_summary FROM audit_event WHERE "
                                "object_public_id=:id AND action='proofreading.audio_approve'"
                            )
                        ),
                        {"id": cohort["public_id"]},
                    )
                )
                .scalars()
                .all()
            )
            summaries = [json.loads(value) if isinstance(value, str) else value for value in audits]
            assert len(summaries) == 3
            assert {item["method"] for item in summaries} == {"MANUAL", "BATCH_NORMAL"}
            assert all(item["actor"] == actor and item["reason"] for item in summaries)
        for draft in drafts[:3]:
            checks = await production.checks(draft.id)
            assert all(item.passed for item in checks)
        assert not all(item.passed for item in await production.checks(drafts[-1].id))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["slice_file", "sample_safety", "sample_failure", "source_dependency"]
)
async def test_invalidated_sample_or_late_failure_rolls_back_approval(change):
    engine, sessions = database()
    try:
        proof, _production, actor, cohort, drafts, audio_ids = await prepared_cohort(sessions)
        frozen = {item["revision_id"]: item for item in cohort["candidates"]}
        sample = frozen[cohort["sample_ids"][0]]
        await proof.confirm(
            cohort["public_id"],
            sample["revision_id"],
            sample["candidate_hash"],
            actor,
            {"text_checked": True, "audio_listened": True},
        )
        selected = [draft for draft in drafts if draft.id != sample["revision_id"]]
        if change == "sample_failure":
            await proof.fail(
                cohort["public_id"], sample["revision_id"], "AUDIO_TEXT_MISMATCH", actor
            )
        else:
            async with sessions() as session, session.begin():
                if change == "slice_file":
                    # 先选正常项、再选坏项,验证前一项的音频核准和审计也会完整回滚
                    await session.execute(
                        text(
                            "UPDATE sentence_audio_slice SET actual_duration_ms=999 "
                            "WHERE revision_id=:id"
                        ),
                        {"id": selected[1].id},
                    )
                elif change == "source_dependency":
                    await session.execute(
                        text(
                            "UPDATE sentence_audio_slice SET source_sha256=:sha WHERE "
                            "revision_id=:id"
                        ),
                        {"id": selected[1].id, "sha": "f" * 64},
                    )
                else:
                    await session.execute(
                        text(
                            "UPDATE media_asset m JOIN sentence_audio_slice s ON "
                            "s.asset_id=m.public_id SET m.security_status='BLOCKED' "
                            "WHERE s.revision_id=:id"
                        ),
                        {"id": sample["revision_id"]},
                    )
        with pytest.raises(AppError) as rejected:
            await proof.bulk_confirm(
                cohort["public_id"],
                actor,
                cohort["collection_hash"],
                [draft.id for draft in selected],
                True,
                uuid4().hex,
            )
        assert rejected.value.code in {
            "PROOFREADING_MATERIAL_CHANGED",
            "PROOFREADING_CHECK_REQUIRED",
            "PROOFREADING_SAMPLE_REQUIRED",
        }
        async with sessions() as session:
            for draft in selected:
                versions = audio_ids[drafts.index(draft)][:2]
                states = (
                    (
                        await session.execute(
                            text(
                                "SELECT status FROM audio_version "
                                "WHERE public_id IN (:whole,:slice)"
                            ),
                            {"whole": versions[0], "slice": versions[1]},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert states == ["READY", "READY"]
            assert not (
                await session.execute(
                    text(
                        "SELECT revision_id FROM proofreading_confirmation WHERE "
                        "set_id=:id AND method='BATCH_NORMAL'"
                    ),
                    {"id": cohort["public_id"]},
                )
            ).first()
            assert (
                await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM audit_event WHERE object_public_id=:id "
                        "AND action='proofreading.audio_approve'"
                    ),
                    {"id": cohort["public_id"]},
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unconfirmed_entry_cannot_use_prepared_audio_to_bypass_content_confirmation():
    from juya_admin_api.modules.content.v15_authoring import SceneAuthoringStore
    from juya_admin_api.modules.content_import.stages import StageStore

    engine, sessions = database()
    try:
        proof, production, actor, _cohort, drafts, _audio_ids = await prepared_cohort(sessions)
        draft = drafts[0]
        snapshot = draft.content | {
            "vocabulary": [
                {
                    "english": "Hello",
                    "chinese": "你好",
                    "source_sentence_ids": ["sentence"],
                    "source_sentence": "Hello.",
                }
            ]
        }
        updated = await production.save_revision(
            SceneRevision(draft.id, draft.scene_id, None, content=snapshot), draft.version
        )
        async with sessions() as session, session.begin():
            facts = await proof.default_facts(session, "dialogue", draft.id, updated.content)
        assert facts["ready"] is False and "ENTRY_REFERENCE_INVALID" in facts["error_codes"]
        authoring = SceneAuthoringStore(sessions, proof, StageStore(sessions), require_review=False)
        confirmed = await authoring.confirm_entries_bulk(
            [
                {
                    "revision_id": draft.id,
                    "expected_version": updated.version,
                    "candidate_hash": snapshot_hash(updated.content),
                }
            ],
            actor,
            True,
        )
        assert confirmed["status"] == "CONFIRMED"
        fixed = confirmed["items"][0]["content"]
        fresh = await proof.create(
            [
                {
                    "content_type": "dialogue",
                    "content_id": draft.scene_id,
                    "revision_id": draft.id,
                    "candidate_hash": snapshot_hash(fixed),
                }
            ],
            actor,
        )
        assert fresh["normal_count"] == 1
    finally:
        await engine.dispose()
