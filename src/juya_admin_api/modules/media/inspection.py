import asyncio
import hashlib
import json
import math
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class InspectedMedia:
    content_type: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None


async def inspect_media(
    data: bytes, asset_type: str, ffprobe_path: str = "ffprobe"
) -> InspectedMedia:
    # 功能:在线程池中解码素材并读取真实类型、大小、摘要及尺寸或时长。
    # 参数:
    #     data: 待检查的素材原始字节,计算摘要并解码真实类型、尺寸或时长。
    #     asset_type: 素材分类,图片为 images,音频为 audio。
    #     ffprobe_path: ffprobe 可执行文件路径或命令名,读取音频格式和时长。
    # 返回:素材真实 MIME、字节数、摘要及像素尺寸或毫秒时长。
    return await asyncio.to_thread(_inspect, data, asset_type, ffprobe_path)


def _inspect(data: bytes, asset_type: str, ffprobe_path: str) -> InspectedMedia:
    # 功能:解码图片或调用 ffprobe 检查音频,拒绝格式错误和超限素材。
    # 参数:
    #     data: 待检查的素材原始字节,计算摘要并解码真实类型、尺寸或时长。
    #     asset_type: 素材分类,图片为 images,音频为 audio。
    #     ffprobe_path: ffprobe 可执行文件路径或命令名,读取音频格式和时长。
    # 返回:素材真实 MIME、字节数、摘要及像素尺寸或毫秒时长。
    digest = hashlib.sha256(data).hexdigest()
    try:
        if asset_type == "images":
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "BMP": "image/bmp",
                        "WEBP": "image/webp",
                    }.get(image.format or "")
                    if mime is None or image.width * image.height > 40_000_000:
                        raise ValueError("unsupported image")
                    width, height = image.size
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    image.load()
            return InspectedMedia(mime, len(data), digest, width, height)
        with tempfile.TemporaryDirectory(prefix="juya-media-") as directory:
            path = Path(directory) / "asset"
            path.write_bytes(data)
            result = subprocess.run(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-count_frames",
                    "-show_entries",
                    "format=duration,format_name:stream=codec_type,nb_read_frames",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            if result.stderr.strip():
                raise ValueError("audio decoder reported errors")
            info = json.loads(result.stdout)
        if not any(
            stream.get("codec_type") == "audio" and int(stream.get("nb_read_frames", "0")) > 0
            for stream in info.get("streams", [])
        ):
            raise ValueError("missing decoded audio frames")
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration) or not 0 < duration <= 7200:
            raise ValueError("invalid duration")
        formats = str(info["format"]["format_name"]).split(",")
        mime = next(
            (
                value
                for name, value in [
                    ("mp3", "audio/mpeg"),
                    ("wav", "audio/wav"),
                    ("mov", "audio/mp4"),
                    ("aac", "audio/aac"),
                ]
                if name in formats
            ),
            None,
        )
        if mime is None:
            raise ValueError("unsupported audio")
        return InspectedMedia(mime, len(data), digest, duration_ms=round(duration * 1000))
    except FileNotFoundError:
        raise AppError("MEDIA_INSPECTOR_UNAVAILABLE", "服务器未配置 ffprobe", 503) from None
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        subprocess.SubprocessError,
    ):
        raise AppError("MEDIA_DECODE_FAILED", "素材无法解码或超出检查限制", 422) from None
