"""Current dependency evidence for task history projection, without scheduling work."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_candidates import dependency


async def stage_dependencies(
    session: AsyncSession, revision: str, content: SceneContent, version: int
) -> tuple[dict[str, bool], dict[str, str]]:
    source_sha = (
        await session.scalar(
            text("SELECT sha256 FROM media_asset WHERE public_id=:id"),
            {"id": content.audio.asset_id},
        )
        if content.audio
        else ""
    )
    rows = (
        (
            await session.execute(
                text(
                    "SELECT public_id,input_hash,candidate FROM lexicon_candidate "
                    "WHERE revision_id=:id"
                ),
                {"id": revision},
            )
        )
        .mappings()
        .all()
    )
    stages = {}
    for row in rows:
        payload = decode(row["candidate"])
        kind = payload.get("kind")
        if kind not in {"AI_LEXICON", "ASR_BOUNDARY"}:
            continue
        current = dependency(
            content, version, kind, str(source_sha or "") if kind == "ASR_BOUNDARY" else ""
        )
        expected = (
            payload.get("decision_input_hash")
            or payload.get("applied_input_hash")
            or row["input_hash"]
        )
        stages[row["public_id"]] = current == expected
        if payload.get("poll_stage_id"):
            stages[payload["poll_stage_id"]] = current == expected
    slices = {}
    for line in content.dialogue:
        if (
            content.audio
            and source_sha
            and line.start_ms is not None
            and line.end_ms is not None
            and 0 <= line.start_ms < line.end_ms
        ):
            slices[line.id] = dependency_hash(
                sentence_id=line.id,
                english=line.english,
                source_sha256=source_sha,
                source_audio_version=content.audio.version_id,
                boundary=Boundary(line.start_ms, line.end_ms),
                encoding_config=FFmpegSlicer.encoding_config,
            )
    return stages, slices
