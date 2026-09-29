from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class AdminUser:
    id: int
    public_id: str
    username: str
    password_hash: str = field(repr=False)
    status: str = "ACTIVE"
    failed_login_count: int = 0
    locked_until: datetime | None = None


@dataclass(slots=True)
class SessionRecord:
    id: str
    admin_user_id: int
    token_hash: str
    csrf_hash: str
    device_summary: str
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AdminSession:
    id: str
    token: str
    csrf_token: str
    expires_at: datetime
