from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TtsResult:
    provider_request_id: str
    object_key: str
    duration_ms: int


class TtsProvider(Protocol):
    async def synthesize(self, audio_target: str, voice: str, text: str) -> TtsResult: ...
