import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from juya_admin_api.modules.audio_pipeline.slicing import Boundary


@dataclass(frozen=True)
class ASRCandidate:
    text: str
    boundaries: tuple[Boundary, ...]
    requires_manual_alignment: bool = True


class AliyunASR:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        *,
        base_url: str = "https://dashscope.aliyuncs.com/api/v1",
        model: str = "qwen3-asr-flash-filetrans",
        result_hosts: tuple[str, ...] = ("dashscope-result.oss-cn-beijing.aliyuncs.com",),
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
        self._validate_url(transcription_url, self.result_hosts)
        result = await self._request("GET", transcription_url)
        texts: list[str] = []
        boundaries: list[Boundary] = []
        for transcript in result.get("transcripts", []):
            texts.append(str(transcript.get("text", "")))
            for sentence in transcript.get("sentences", []):
                start, end = sentence.get("begin_time"), sentence.get("end_time")
                if type(start) is int and type(end) is int and start >= 0 and end > start:
                    boundaries.append(Boundary(start, end))
        # Timestamp existence does not establish correspondence to administrator sentence IDs.
        return ASRCandidate("\n".join(texts), tuple(boundaries))
