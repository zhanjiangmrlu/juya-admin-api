import hashlib
import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class Boundary:
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or self.start_ms < 0
            or self.end_ms <= self.start_ms
        ):
            raise ValueError("invalid sentence boundary")


@dataclass(frozen=True)
class SliceResult:
    data: bytes
    duration_ms: int
    sha256: str
    content_type: str = "audio/wav"


class FFmpegSlicer:
    # WAV PCM avoids encoder padding; all inputs are private bytes, never URLs or paths.
    encoding_config: ClassVar[dict[str, object]] = {
        "codec": "pcm_s16le",
        "rate": 24000,
        "channels": 1,
        "format": "wav",
    }

    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    def _run(self, command: list[str]) -> bytes:
        try:
            process = subprocess.run(command, capture_output=True, timeout=180, check=True)
            if process.stderr.strip():
                raise ValueError("audio decoder reported errors")
            return process.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("audio processing unavailable or invalid") from exc

    def _inspect(self, path: Path) -> int:
        raw = self._run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mp3,mov,wav,aac",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ]
        )
        info = json.loads(raw)
        if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
            raise ValueError("missing audio stream")
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration) or not 0 < duration <= 7200:
            raise ValueError("invalid audio duration")
        # Probe metadata is insufficient: decode the complete stream with strict errors.
        self._run(
            [
                self.ffmpeg_path,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "mp3,mov,wav,aac",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ]
        )
        return round(duration * 1000)

    def slice(self, source: bytes, source_sha256: str, boundary: Boundary) -> SliceResult:
        if hashlib.sha256(source).hexdigest() != source_sha256:
            raise ValueError("source hash changed")
        if not source or len(source) > 50 * 1024 * 1024:
            raise ValueError("source byte limit")
        with tempfile.TemporaryDirectory(prefix="juya-private-audio-") as directory:
            original = Path(directory) / "source"
            output = Path(directory) / "slice.wav"
            original.write_bytes(source)
            duration = self._inspect(original)
            if boundary.end_ms > duration:
                raise ValueError("boundary exceeds source duration")
            self._run(
                [
                    self.ffmpeg_path,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mp3,mov,wav,aac",
                    "-i",
                    str(original),
                    "-ss",
                    str(boundary.start_ms / 1000),
                    "-t",
                    str((boundary.end_ms - boundary.start_ms) / 1000),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    str(output),
                ]
            )
            actual = self._inspect(output)
            if abs(actual - (boundary.end_ms - boundary.start_ms)) > 30:
                raise ValueError("slice duration mismatch")
            data = output.read_bytes()
        return SliceResult(data, actual, hashlib.sha256(data).hexdigest())


def dependency_hash(
    *,
    sentence_id: str,
    english: str,
    source_sha256: str,
    source_audio_version: str,
    boundary: Boundary,
    encoding_config: dict[str, object],
) -> str:
    payload = {
        "sentence_id": sentence_id,
        "english": english,
        "source_sha256": source_sha256,
        "source_audio_version": source_audio_version,
        "start_ms": boundary.start_ms,
        "end_ms": boundary.end_ms,
        "encoding_config": encoding_config,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
