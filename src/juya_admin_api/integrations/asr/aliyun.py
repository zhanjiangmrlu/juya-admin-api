import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from juya_admin_api.modules.audio_pipeline.slicing import Boundary


@dataclass(frozen=True)
class ASRWord:
    text: str
    start_ms: int | None
    end_ms: int | None
    punctuation: str = ""


@dataclass(frozen=True)
class ASRSegment:
    text: str
    start_ms: int | None
    end_ms: int | None
    words: tuple[ASRWord, ...] = ()
    channel_id: int | None = None


@dataclass(frozen=True)
class ASRCandidate:
    text: str
    boundaries: tuple[Boundary, ...]
    requires_manual_alignment: bool = True
    segments: tuple[ASRSegment, ...] = ()
    issues: tuple[str, ...] = ()


class AliyunASR:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        base_url: str = "https://dashscope.aliyuncs.com/api/v1",
        model: str = "qwen3-asr-flash-filetrans",
        result_hosts: tuple[str, ...] = (
            "dashscope-result.oss-cn-beijing.aliyuncs.com",
            "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        ),
        audio_hosts: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.result_hosts = result_hosts
        self.audio_hosts = audio_hosts

    @staticmethod
    def _validate_url(url: str, hosts: tuple[str, ...]) -> None:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in hosts
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise ValueError("provider URL not allowed")

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, url, timeout=45, follow_redirects=False, **kwargs
            )
            response.raise_for_status()
            if len(response.content) > 5 * 1024 * 1024:
                raise ValueError("response size")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("response schema")
            return result
        except (httpx.HTTPError, ValueError):
            raise ValueError("ASR provider request failed") from None

    async def submit(self, signed_audio_url: str) -> str:
        self._validate_url(signed_audio_url, self.audio_hosts)
        result = await self._request(
            "POST",
            self.base_url + "/services/audio/asr/transcription",
            headers={"Authorization": f"Bearer {self._key}", "X-DashScope-Async": "enable"},
            json={
                "model": self.model,
                "input": {"file_url": signed_audio_url},
                "parameters": {"channel_id": [0], "enable_itn": False, "enable_words": True},
            },
        )
        task_id = str(result.get("output", {}).get("task_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("ASR missing task ID")
        return task_id

    async def poll(self, task_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("ASR invalid task ID")
        return await self._request(
            "GET",
            self.base_url + "/tasks/" + task_id,
            headers={"Authorization": f"Bearer {self._key}"},
        )

    async def parse_result(self, transcription_url: str) -> ASRCandidate:
        # 功能:保留原转录文本、channel及真实段/词时间,异常明确诊断而不静默丢弃后自动对齐。
        parts = urlsplit(transcription_url)
        if parts.scheme == "http" and parts.hostname in self.result_hosts and parts.port is None:
            # Official OSS result links may use HTTP. Send only HTTPS to the exact trusted host.
            transcription_url = parts._replace(scheme="https").geturl()
        self._validate_url(transcription_url, self.result_hosts)
        result = await self._request("GET", transcription_url)
        texts: list[str] = []
        boundaries: list[Boundary] = []
        segments: list[ASRSegment] = []
        issues: list[str] = []
        transcripts = result.get("transcripts")
        if not isinstance(transcripts, list) or not transcripts:
            return ASRCandidate("", (), issues=("ASR_TRANSCRIPTS_MISSING",))
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                issues.append("ASR_TRANSCRIPT_INVALID")
                continue
            transcript_text = transcript.get("text")
            if isinstance(transcript_text, str):
                texts.append(transcript_text)
            else:
                issues.append("ASR_TRANSCRIPT_TEXT_INVALID")
            channel = transcript.get("channel_id")
            if type(channel) is not int or channel != 0:
                issues.append("ASR_CHANNEL_INVALID")
            sentences = transcript.get("sentences")
            if not isinstance(sentences, list) or not sentences:
                issues.append("ASR_SEGMENTS_MISSING")
                continue
            for sentence in sentences:
                if not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str):
                    issues.append("ASR_SEGMENT_INVALID")
                    continue
                start, end = sentence.get("begin_time"), sentence.get("end_time")
                if type(start) is int and type(end) is int and start >= 0 and end > start:
                    boundaries.append(Boundary(start, end))
                words: list[ASRWord] = []
                raw_words = sentence.get("words", [])
                if not isinstance(raw_words, list):
                    issues.append("ASR_WORDS_INVALID")
                    raw_words = []
                for word in raw_words:
                    if not isinstance(word, dict) or not isinstance(word.get("text"), str):
                        issues.append("ASR_WORD_INVALID")
                        continue
                    punctuation = word.get("punctuation", "")
                    if not isinstance(punctuation, str):
                        issues.append("ASR_WORD_PUNCTUATION_INVALID")
                        punctuation = ""
                    words.append(
                        ASRWord(
                            word["text"],
                            word.get("begin_time") if type(word.get("begin_time")) is int else None,
                            word.get("end_time") if type(word.get("end_time")) is int else None,
                            punctuation,
                        )
                    )
                segments.append(
                    ASRSegment(
                        sentence["text"],
                        start if type(start) is int else None,
                        end if type(end) is int else None,
                        tuple(words),
                        channel if type(channel) is int else None,
                    )
                )
        return ASRCandidate(
            "\n".join(texts), tuple(boundaries), segments=tuple(segments), issues=tuple(issues)
        )
