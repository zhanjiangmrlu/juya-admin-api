from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.modules.audio_pipeline.repository import (
    CurrentInput,
    LockedInput,
    SQLAudioRepository,
)
from juya_admin_api.modules.audio_pipeline.service import AudioPipelineService
from juya_admin_api.modules.audio_pipeline.slicing import FFmpegSlicer


def create_audio_pipeline(
    *,
    sessions: async_sessionmaker[AsyncSession],
    oss: OssProvider,
    current_input: CurrentInput,
    locked_input: LockedInput,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> AudioPipelineService:
    slicer = FFmpegSlicer(ffmpeg_path, ffprobe_path)
    repository = SQLAudioRepository(sessions, current_input, locked_input, slicer.encoding_config)
    return AudioPipelineService(oss, slicer, repository)
