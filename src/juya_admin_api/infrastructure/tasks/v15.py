"""Recoverable worker dispatch; local stages and charged pronunciation jobs stay separate."""

import asyncio
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.session import create_session_factory
from juya_admin_api.infrastructure.runtime import build_runtime
from juya_admin_api.infrastructure.tasks.celery_app import celery_app
from juya_admin_api.infrastructure.v15_runtime import tts_config
from juya_admin_api.integrations.content_security.aliyun import create_content_security_provider
from juya_admin_api.integrations.tts.tencent import TencentCredentials, TencentTtsProvider
from juya_admin_api.modules.audio_pipeline.factory import create_audio_pipeline
from juya_admin_api.modules.audio_pipeline.service import SliceInput
from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.pronunciation.tasks import PronunciationWorker, dispatch_pending
from juya_admin_api.shared.errors import AppError


@celery_app.task(name="juya.content.publish.v15_dispatch")  # type: ignore[untyped-decorator]
def dispatch() -> dict[str, Any]:
    return asyncio.run(dispatch_async(Settings()))


async def dispatch_async(settings: Settings) -> dict[str, Any]:
    runtime = build_runtime(settings)
    assert runtime.v15
    services = runtime.v15
    sessions = create_session_factory(runtime.engine)
    try:
        for stage_id in await services.imports.stages.due(datetime.now(UTC)):
            await services.imports.stages.enqueue(stage_id, datetime.now(UTC))
        async with sessions() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id,payload FROM admin_outbox WHERE event_type IN "
                            "('content.import.validate','CONTENT_IMPORT_CONFIRM','CONTENT_SENTENCE_SLICE',"
                            "'CONTENT_IMPORT_STAGE') AND ((status='PENDING' AND "
                            "(next_attempt_at IS NULL "
                            "OR next_attempt_at<=UTC_TIMESTAMP(6))) OR (status='PROCESSING' "
                            "AND next_attempt_at<=UTC_TIMESTAMP(6))) ORDER BY id LIMIT 100 FOR "
                            "UPDATE SKIP LOCKED"
                        )
                    )
                )
                .mappings()
                .all()
            )
            expiry = datetime.now(UTC) + timedelta(seconds=60)
            for row in rows:
                await session.execute(
                    text(
                        "UPDATE admin_outbox SET status='PROCESSING',next_attempt_at=:expiry "
                        "WHERE id=:id"
                    ),
                    {"id": row["id"], "expiry": expiry},
                )
        delivered = 0
        for row in rows:
            try:
                process_stage.delay(decode(row["payload"])["stage_id"])
                async with sessions() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE admin_outbox SET "
                            "status='PUBLISHED',published_at=UTC_TIMESTAMP(6) "
                            "WHERE id=:id"
                        ),
                        {"id": row["id"]},
                    )
                delivered += 1
            except Exception:
                async with sessions() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE admin_outbox SET "
                            "status='PENDING',attempt_count=attempt_count+1,"
                            "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 30 SECOND) "
                            "WHERE id=:id"
                        ),
                        {"id": row["id"]},
                    )

        async def send_pronunciation(task_id: str) -> None:
            pronunciation.delay(task_id)

        pron_count = await dispatch_pending(services.pronunciation.repository, send_pronunciation)
        return {"dispatched": delivered, "pronunciation_dispatched": pron_count}
    finally:
        await runtime.close()


@celery_app.task(name="juya.content.publish.v15_stage")  # type: ignore[untyped-decorator]
def process_stage(stage_id: str) -> None:
    asyncio.run(process_stage_async(Settings(), stage_id))


async def process_stage_async(settings: Settings, stage_id: str) -> None:
    runtime = build_runtime(settings)
    assert runtime.v15
    services = runtime.v15
    sessions = create_session_factory(runtime.engine)
    try:
        async with sessions() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM content_import_stage WHERE public_id=:id"),
                        {"id": stage_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            original = await session.scalar(
                text(
                    "SELECT payload FROM admin_outbox WHERE event_type IN "
                    "('CONTENT_IMPORT_CONFIRM','CONTENT_SENTENCE_SLICE') "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.stage_id'))=:id ORDER BY id DESC "
                    "LIMIT 1"
                ),
                {"id": stage_id},
            )
            payload = decode(original)
        if row["stage"] == "VALIDATE":
            await services.imports.validate(row["item_id"], stage_id)
            return
        if str(row["stage"]).startswith("PUBLICATION_RANGE_"):
            assert services.publication is not None
            await services.publication.run(stage_id, uuid4().hex)
            return
        if str(row["stage"]).startswith("MATERIAL_"):
            assert services.materials is not None
            await services.materials.run(stage_id, uuid4().hex, datetime.now(UTC))
            return
        if row["stage"] in {"AI_LEXICON", "ASR_BOUNDARY", "ASR_POLL"}:
            await services.candidates.run(stage_id, uuid4().hex, datetime.now(UTC))
            return
        claim = await services.imports.stages.claim(stage_id, uuid4().hex, datetime.now(UTC), 1800)
        if claim is None:
            return
        try:
            if row["stage"] == "CONFIRM":
                result = await services.imports.confirm(
                    payload["batch_id"],
                    **payload["confirmation"],
                    actor=payload["actor"],
                    key=payload["key"],
                    claim=claim,
                )
            elif row["stage"] == "SENTENCE_SLICE":

                async def locked_input(
                    session: AsyncSession, revision_id: str, sentence_id: str
                ) -> SliceInput:
                    await services.imports.assert_claim_within(session, claim, str(row["item_id"]))
                    return await services.authoring.locked_input(session, revision_id, sentence_id)

                item = await services.authoring.current_input(
                    payload["revision_id"], payload["sentence_id"]
                )
                if item.dependency(services.authoring.encoding_config) != claim.input_hash:
                    raise AppError("SLICE_DEPENDENCY_CHANGED", "切片输入已变化", 409)
                pipeline = create_audio_pipeline(
                    sessions=sessions,
                    oss=services.oss,
                    current_input=services.authoring.current_input,
                    locked_input=locked_input,
                    ffmpeg_path=settings.ffmpeg_path,
                    ffprobe_path=settings.ffprobe_path,
                )
                sliced = await pipeline.split(payload["revision_id"], payload["sentence_id"])
                if services.authoring.prepare_slice:
                    await services.authoring.prepare_slice(sliced.asset_id, sliced.resource_version)
                result = asdict(sliced)
            else:
                raise AppError("IMPORT_STAGE_UNSUPPORTED", "未注册该阶段处理器", 409)
            await services.imports.stages.finish(claim, result, datetime.now(UTC))
        except Exception as error:
            code = error.code if isinstance(error, AppError) else "LOCAL_STAGE_FAILED"
            with suppress(AppError):
                await services.imports.stages.fail(claim, code, datetime.now(UTC), retry=False)
    finally:
        await runtime.close()


@celery_app.task(name="juya.content.audio.pronunciation")  # type: ignore[untyped-decorator]
def pronunciation(task_id: str) -> None:
    asyncio.run(pronunciation_async(Settings(), task_id))


async def pronunciation_async(settings: Settings, task_id: str) -> None:
    runtime = build_runtime(settings)
    assert runtime.v15
    services = runtime.v15
    sessions = create_session_factory(runtime.engine)
    try:
        period = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "INSERT IGNORE INTO budget_reservation(period,character_limit,cost_limit,"
                    "reserved_characters,reserved_cost,enabled) "
                    "VALUES(:period,:chars,:cost,0,0,:enabled)"
                ),
                {
                    "period": period,
                    "chars": settings.tts_daily_character_limit,
                    "cost": settings.tts_daily_microcost_limit,
                    "enabled": tts_config(settings).enabled
                    and settings.tts_daily_character_limit > 0
                    and settings.tts_daily_microcost_limit > 0,
                },
            )

        def credentials() -> TencentCredentials:
            if settings.tts_secret_id is None or settings.tts_secret_key is None:
                raise AppError("TTS_DISABLED", "合成供应商未配置", 409)
            return TencentCredentials(
                settings.tts_secret_id.get_secret_value(),
                settings.tts_secret_key.get_secret_value(),
                settings.tts_session_token.get_secret_value()
                if settings.tts_session_token
                else None,
            )

        worker = PronunciationWorker(
            services.pronunciation.repository,
            TencentTtsProvider(credentials),
            services.oss,
            create_content_security_provider(settings, services.oss),
            settings.ffprobe_path,
            current_config=lambda: tts_config(Settings()),
        )
        await worker.run(task_id)
    finally:
        await runtime.close()
