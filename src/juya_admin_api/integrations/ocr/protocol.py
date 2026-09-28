from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OcrResult:
    provider_request_id: str
    text: str
    blocks: list[dict[str, Any]]


class OcrProvider(Protocol):
    async def recognize(self, object_key: str, template_type: str) -> OcrResult: ...
