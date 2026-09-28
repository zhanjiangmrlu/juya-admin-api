from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class AccessLevel(StrEnum):
    OPEN = "OPEN"
    FORMAL = "FORMAL"
    LIMITED = "LIMITED"
    PREVIEW = "PREVIEW"
    HIDDEN = "HIDDEN"


@dataclass(frozen=True, slots=True)
class AccessGrant:
    source: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AccessDecision:
    level: AccessLevel
    sources: tuple[str, ...]
    earliest_expires_at: datetime | None

    @property
    def has_full_access(self) -> bool:
        return self.level in {AccessLevel.OPEN, AccessLevel.FORMAL, AccessLevel.LIMITED}
