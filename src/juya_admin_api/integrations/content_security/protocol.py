from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SecurityResult:
    provider_request_id: str
    status: str
    labels: tuple[str, ...] = ()


class ContentSecurityProvider(Protocol):
    async def scan_text(self, text: str) -> SecurityResult: ...

    async def scan_image(self, object_key: str) -> SecurityResult: ...
