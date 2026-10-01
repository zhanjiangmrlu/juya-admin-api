import calendar
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid

NaturalMonthCount = Literal[1, 2, 3, 6, 12]
BEIJING = ZoneInfo("Asia/Shanghai")


class EntitlementTerm(StrEnum):
    MONTH_1 = "month_1"
    MONTH_2 = "month_2"
    MONTH_3 = "month_3"
    MONTH_6 = "month_6"
    MONTH_12 = "month_12"
    PERMANENT = "permanent"

    @property
    def months(self) -> NaturalMonthCount | None:
        values: dict[EntitlementTerm, NaturalMonthCount | None] = {
            EntitlementTerm.MONTH_1: 1,
            EntitlementTerm.MONTH_2: 2,
            EntitlementTerm.MONTH_3: 3,
            EntitlementTerm.MONTH_6: 6,
            EntitlementTerm.MONTH_12: 12,
            EntitlementTerm.PERMANENT: None,
        }
        return values[self]


class EntitlementOperation(StrEnum):
    GRANT = "GRANT"
    RENEW = "RENEW"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    REVOKE = "REVOKE"


@dataclass(slots=True)
class FormalEntitlement:
    id: str
    user_id: str
    package_id: str
    status: str
    term: EntitlementTerm
    granted_at: datetime
    expires_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class FormalEntitlementCommand:
    user_id: str
    package_id: str
    operation: EntitlementOperation
    term: EntitlementTerm | None = None
    reason: str | None = None


def add_natural_months(base_utc: datetime, months: NaturalMonthCount) -> datetime:
    if base_utc.tzinfo is None:
        raise ValueError("base_utc must be timezone-aware")
    local = base_utc.astimezone(BEIJING)
    absolute_month = local.year * 12 + local.month - 1 + months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    shifted = local.replace(year=year, month=month, day=day)
    return shifted.astimezone(UTC)


def command_hash(command: FormalEntitlementCommand) -> str:
    payload = asdict(command)
    payload["operation"] = command.operation.value
    payload["term"] = None if command.term is None else command.term.value
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def calculate_operation(
    current: FormalEntitlement | None,
    command: FormalEntitlementCommand,
    now: datetime,
) -> FormalEntitlement:
    if command.operation in {EntitlementOperation.GRANT, EntitlementOperation.RENEW}:
        return _calculate_grant_or_renew(current, command, now)
    if current is None:
        raise AppError("ENTITLEMENT_NOT_FOUND", "正式权益不存在", 404)
    if command.operation is EntitlementOperation.PAUSE:
        if current.status != "ACTIVE" or (
            current.expires_at is not None and now >= current.expires_at
        ):
            raise AppError("ENTITLEMENT_STATE_CONFLICT", "当前状态不可暂停", 409)
        return replace(current, status="PAUSED", version=current.version + 1)
    if command.operation is EntitlementOperation.RESUME:
        if current.status != "PAUSED":
            raise AppError("ENTITLEMENT_STATE_CONFLICT", "当前状态不可恢复", 409)
        if current.expires_at is not None and now >= current.expires_at:
            raise AppError("ENTITLEMENT_EXPIRED", "权益已到期, 不可恢复", 409)
        return replace(current, status="ACTIVE", version=current.version + 1)
    if command.operation is EntitlementOperation.REVOKE:
        if current.status not in {"ACTIVE", "PAUSED"}:
            raise AppError("ENTITLEMENT_STATE_CONFLICT", "当前状态不可撤销", 409)
        return replace(current, status="REVOKED", version=current.version + 1)
    raise AppError("ENTITLEMENT_OPERATION_INVALID", "不支持的权益操作", 422)


def _calculate_grant_or_renew(
    current: FormalEntitlement | None,
    command: FormalEntitlementCommand,
    now: datetime,
) -> FormalEntitlement:
    if command.term is None:
        raise AppError("ENTITLEMENT_TERM_REQUIRED", "授予或续期必须选择期限", 422)
    if (
        current is not None
        and current.status != "REVOKED"
        and current.term is EntitlementTerm.PERMANENT
    ):
        raise AppError(
            "PERMANENT_ENTITLEMENT_CANNOT_EXTEND",
            "永久权益不能叠加期限",
            409,
        )
    existing_is_current = (
        current is not None
        and current.status in {"ACTIVE", "PAUSED"}
        and (current.expires_at is None or now < current.expires_at)
    )
    if command.term is EntitlementTerm.PERMANENT:
        expires_at = None
    else:
        assert command.term.months is not None
        base = (
            current.expires_at
            if existing_is_current and current is not None and current.expires_at is not None
            else now
        )
        expires_at = add_natural_months(base, command.term.months)
    return FormalEntitlement(
        id=new_ulid(now) if current is None else current.id,
        user_id=command.user_id,
        package_id=command.package_id,
        status="ACTIVE",
        term=command.term,
        granted_at=(current.granted_at if existing_is_current and current is not None else now),
        expires_at=expires_at,
        version=1 if current is None else current.version + 1,
    )
