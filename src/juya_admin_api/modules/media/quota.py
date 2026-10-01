import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class OcrSettings:
    enabled: bool = False
    monthly_limit: int = 0
    free_quota: int = 0
    paid_disabled: bool = False
    quota_verified_at: datetime | None = None
    updated_by: str | None = None
    updated_at: datetime | None = None


def month_of(now: datetime) -> str:
    return (
        (now if now.tzinfo else now.replace(tzinfo=UTC))
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%Y-%m")
    )


def validate_settings(settings: OcrSettings, now: datetime) -> None:
    if not settings.enabled:
        raise AppError("OCR_DISABLED", "OCR 默认关闭, 请先核实免费额度与关闭付费调用", 409)
    if (
        settings.quota_verified_at is None
        or month_of(settings.quota_verified_at) != month_of(now)
        or not settings.paid_disabled
        or settings.monthly_limit > settings.free_quota
    ):
        raise AppError("OCR_QUOTA_UNVERIFIED", "本月免费额度或付费关闭状态未核实", 409)


class OcrQuotaRepository(Protocol):
    async def settings(self) -> OcrSettings: ...
    async def configure(self, settings: OcrSettings) -> None: ...
    async def usage(self, month: str) -> int: ...
    async def reserve(self, job_id: str, now: datetime) -> None: ...


class InMemoryOcrQuotaRepository:
    def __init__(self) -> None:
        self.value = OcrSettings()
        self.counts: dict[str, int] = {}
        self.reservations: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def settings(self) -> OcrSettings:
        return self.value

    async def configure(self, settings: OcrSettings) -> None:
        async with self.lock:
            self.value = settings

    async def usage(self, month: str) -> int:
        return self.counts.get(month, 0)

    async def reserve(self, job_id: str, now: datetime) -> None:
        async with self.lock:
            validate_settings(self.value, now)
            if job_id in self.reservations:
                if self.reservations[job_id] != month_of(now):
                    raise AppError("OCR_RESERVATION_EXPIRED", "OCR预占已跨月失效", 409)
                return
            month = month_of(now)
            count = self.counts.get(month, 0)
            if count >= self.value.monthly_limit:
                raise AppError("OCR_QUOTA_EXHAUSTED", "本月 OCR 内部额度已用完", 409)
            self.counts[month] = count + 1
            self.reservations[job_id] = month


class SQLAlchemyOcrQuotaRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def settings(self) -> OcrSettings:
        async with self.sessions() as session:
            row = (
                (await session.execute(text("SELECT * FROM ocr_settings WHERE id=1")))
                .mappings()
                .one()
            )
            return _settings(dict(row))

    async def configure(self, settings: OcrSettings) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE ocr_settings SET enabled=:enabled, monthly_limit=:monthly_limit, "
                    "free_quota=:free_quota, paid_disabled=:paid_disabled, "
                    "quota_verified_at=:quota_verified_at, updated_by=:updated_by, "
                    "updated_at=:updated_at WHERE id=1"
                ),
                asdict(settings),
            )

    async def usage(self, month: str) -> int:
        async with self.sessions() as session:
            value = await session.scalar(
                text("SELECT reserved_count FROM ocr_monthly_usage WHERE month=:month"),
                {"month": month},
            )
            return int(value or 0)

    async def reserve(self, job_id: str, now: datetime) -> None:
        async with self.sessions() as session, session.begin():
            row = (
                (await session.execute(text("SELECT * FROM ocr_settings WHERE id=1 FOR UPDATE")))
                .mappings()
                .one()
            )
            existing = await session.scalar(
                text("SELECT month FROM ocr_quota_reservation WHERE job_public_id=:job_id"),
                {"job_id": job_id},
            )
            settings = _settings(dict(row))
            validate_settings(settings, now)
            if existing:
                if existing != month_of(now):
                    raise AppError("OCR_RESERVATION_EXPIRED", "OCR预占已跨月失效", 409)
                return
            month = month_of(now)
            await session.execute(
                text(
                    "INSERT INTO ocr_monthly_usage(month,reserved_count) VALUES(:month,0) "
                    "ON DUPLICATE KEY UPDATE month=VALUES(month)"
                ),
                {"month": month},
            )
            count = await session.scalar(
                text("SELECT reserved_count FROM ocr_monthly_usage WHERE month=:month FOR UPDATE"),
                {"month": month},
            )
            if int(count or 0) >= settings.monthly_limit:
                raise AppError("OCR_QUOTA_EXHAUSTED", "本月 OCR 内部额度已用完", 409)
            await session.execute(
                text(
                    "INSERT INTO ocr_quota_reservation(job_public_id,month,created_at) "
                    "VALUES(:job_id,:month,:now)"
                ),
                {"job_id": job_id, "month": month, "now": now},
            )
            await session.execute(
                text(
                    "UPDATE ocr_monthly_usage SET reserved_count=reserved_count+1 "
                    "WHERE month=:month"
                ),
                {"month": month},
            )


def _settings(row: dict[str, object]) -> OcrSettings:
    return OcrSettings(**{name: row[name] for name in OcrSettings.__dataclass_fields__})  # type: ignore[arg-type]


class OcrQuotaService:
    def __init__(self, repository: OcrQuotaRepository) -> None:
        self.repository = repository

    async def status(self, now: datetime) -> dict[str, object]:
        settings = await self.repository.settings()
        month = month_of(now)
        used = await self.repository.usage(month)
        return asdict(settings) | {
            "month": month,
            "reserved_count": used,
            "remaining": max(settings.monthly_limit - used, 0),
        }

    async def reserve(self, job_id: str, now: datetime) -> None:
        await self.repository.reserve(job_id, now)

    async def configure(
        self,
        *,
        enabled: bool,
        monthly_limit: int,
        free_quota: int,
        paid_disabled: bool,
        verify_quota: bool,
        actor_id: str,
        now: datetime,
    ) -> dict[str, object]:
        if not 0 <= monthly_limit <= free_quota <= 1_000_000:
            raise AppError("OCR_QUOTA_INVALID", "内部额度必须小于已核实免费额度", 422)
        previous = await self.repository.settings()
        value = OcrSettings(
            enabled,
            monthly_limit,
            free_quota,
            paid_disabled,
            now if verify_quota else previous.quota_verified_at,
            actor_id,
            now,
        )
        if enabled:
            validate_settings(value, now)
        await self.repository.configure(value)
        return await self.status(now)
