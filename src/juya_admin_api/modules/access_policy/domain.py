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
    activated_at: datetime | None = None

    @property
    def has_full_access(self) -> bool:
        # 功能: 判断访问级别是否允许读取完整教学内容.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 访问级别属于 OPEN,FORMAL 或 LIMITED 时为 True.
        return self.level in {AccessLevel.OPEN, AccessLevel.FORMAL, AccessLevel.LIMITED}
