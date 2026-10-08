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
    # 功能:按北京时间将操作时间换算为 OCR 额度的月份标识。
    # 参数:
    #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
    # 返回:北京时间月份标识,格式为 YYYY-MM。
    return (
        (now if now.tzinfo else now.replace(tzinfo=UTC))
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .strftime("%Y-%m")
    )


def validate_settings(settings: OcrSettings, now: datetime) -> None:
    # 功能:核对 OCR 已启用、本月免费额度已确认且付费调用已关闭。
    # 参数:
    #     settings: OCR 启用、额度上限、付费关闭和核实时间的配置对象。
    #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
    # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
    async def settings(self) -> OcrSettings:
        # 功能:读取 OCR 启用、免费额度及付费关闭配置。
        # 参数:
        #     self: 当前 OcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:OCR 调用策略及免费额度配置。
        ...

    async def configure(self, settings: OcrSettings) -> None:
        # 功能:保存 OCR 额度及调用策略配置。
        # 参数:
        #     self: 当前 OcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     settings: OCR 启用、额度上限、付费关闭和核实时间的配置对象。
        # 返回:无返回值;保存 OCR 配置。
        ...

    async def usage(self, month: str) -> int:
        # 功能:读取指定月份已预占的 OCR 调用次数。
        # 参数:
        #     self: 当前 OcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     month: 按北京时间计算的额度月份,格式为 YYYY-MM。
        # 返回:指定月份已预占的 OCR 调用次数。
        ...

    async def reserve(self, job_id: str, now: datetime) -> None:
        # 功能:为 OCR 任务幂等预占本月调用额度,阻止跨月复用及额度超限。
        # 参数:
        #     self: 当前 OcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        ...


class InMemoryOcrQuotaRepository:
    def __init__(self) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 InMemoryOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.value = OcrSettings()
        self.counts: dict[str, int] = {}
        self.reservations: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def settings(self) -> OcrSettings:
        # 功能:读取 OCR 启用、免费额度及付费关闭配置。
        # 参数:
        #     self: 当前 InMemoryOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:OCR 调用策略及免费额度配置。
        return self.value

    async def configure(self, settings: OcrSettings) -> None:
        # 功能:保存 OCR 额度及调用策略配置。
        # 参数:
        #     self: 当前 InMemoryOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     settings: OCR 启用、额度上限、付费关闭和核实时间的配置对象。
        # 返回:无返回值;保存 OCR 配置。
        async with self.lock:
            self.value = settings

    async def usage(self, month: str) -> int:
        # 功能:读取指定月份已预占的 OCR 调用次数。
        # 参数:
        #     self: 当前 InMemoryOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     month: 按北京时间计算的额度月份,格式为 YYYY-MM。
        # 返回:指定月份已预占的 OCR 调用次数。
        return self.counts.get(month, 0)

    async def reserve(self, job_id: str, now: datetime) -> None:
        # 功能:为 OCR 任务幂等预占本月调用额度,阻止跨月复用及额度超限。
        # 参数:
        #     self: 当前 InMemoryOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 SQLAlchemyOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     sessions: 异步数据库会话工厂,为事务内的内容或额度读写提供会话。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.sessions = sessions

    async def settings(self) -> OcrSettings:
        # 功能:读取 OCR 启用、免费额度及付费关闭配置。
        # 参数:
        #     self: 当前 SQLAlchemyOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        # 返回:OCR 调用策略及免费额度配置。
        async with self.sessions() as session:
            row = (
                (await session.execute(text("SELECT * FROM ocr_settings WHERE id=1")))
                .mappings()
                .one()
            )
            return _settings(dict(row))

    async def configure(self, settings: OcrSettings) -> None:
        # 功能:保存 OCR 额度及调用策略配置。
        # 参数:
        #     self: 当前 SQLAlchemyOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     settings: OCR 启用、额度上限、付费关闭和核实时间的配置对象。
        # 返回:无返回值;保存 OCR 配置。
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
        # 功能:读取指定月份已预占的 OCR 调用次数。
        # 参数:
        #     self: 当前 SQLAlchemyOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     month: 按北京时间计算的额度月份,格式为 YYYY-MM。
        # 返回:指定月份已预占的 OCR 调用次数。
        async with self.sessions() as session:
            value = await session.scalar(
                text("SELECT reserved_count FROM ocr_monthly_usage WHERE month=:month"),
                {"month": month},
            )
            return int(value or 0)

    async def reserve(self, job_id: str, now: datetime) -> None:
        # 功能:为 OCR 任务幂等预占本月调用额度,阻止跨月复用及额度超限。
        # 参数:
        #     self: 当前 SQLAlchemyOcrQuotaRepository 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
    # 功能:将数据库配置行转换为 OCR 额度配置对象,并将开关的 0/1 转为布尔值
    # 参数:
    #     row: 数据库查询行,包含构造OCR 调用策略及免费额度配置所需的字段。
    # 返回:OCR 调用策略及免费额度配置。
    values = {name: row[name] for name in OcrSettings.__dataclass_fields__}
    values["enabled"] = bool(row["enabled"])
    values["paid_disabled"] = bool(row["paid_disabled"])
    return OcrSettings(**values)  # type: ignore[arg-type]


class OcrQuotaService:
    def __init__(self, repository: OcrQuotaRepository) -> None:
        # 功能:初始化实例依赖、策略和内部状态。
        # 参数:
        #     self: 当前 OcrQuotaService 实例,持有本方法访问的依赖和业务状态。
        #     repository: OCR 额度仓储,保存配置及当月调用次数和任务预占记录。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
        self.repository = repository

    async def status(self, now: datetime) -> dict[str, object]:
        # 功能:读取北京时间当月 OCR 配置、已预占次数和剩余额度。
        # 参数:
        #     self: 当前 OcrQuotaService 实例,持有本方法访问的依赖和业务状态。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:OCR 配置、当月已预占次数和非负剩余额度。
        settings = await self.repository.settings()
        month = month_of(now)
        used = await self.repository.usage(month)
        return asdict(settings) | {
            "month": month,
            "reserved_count": used,
            "remaining": max(settings.monthly_limit - used, 0),
        }

    async def reserve(self, job_id: str, now: datetime) -> None:
        # 功能:为 OCR 任务幂等预占本月调用额度,阻止跨月复用及额度超限。
        # 参数:
        #     self: 当前 OcrQuotaService 实例,持有本方法访问的依赖和业务状态。
        #     job_id: 媒体处理作业公开标识,关联 OCR 或语音生成结果。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:无返回值;完成上述操作或在不满足条件时抛出异常。
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
        # 功能:保存 OCR 额度及调用策略配置。
        # 参数:
        #     self: 当前 OcrQuotaService 实例,持有本方法访问的依赖和业务状态。
        #     enabled: 是否启用 OCR 调用或后台任务调度,具体由当前实例策略决定。
        #     monthly_limit: OCR 内部月调用上限,必须不大于核实的免费调用次数。
        #     free_quota: 已核实的 OCR 月免费调用次数,不超过一百万次。
        #     paid_disabled: 供应商侧是否已关闭付费调用,启用 OCR 前必须为真。
        #     verify_quota: 是否将本次操作时间记录为免费额度核实时间。
        #     actor_id: 发起操作的管理员公开标识,写入创建记录、回执或审计。
        #     now: 当前操作时间,供状态期限判断、额度月份换算及记录时间;通常为 UTC。
        # 返回:配置后的 OCR 当月状态;仓储写入方法无返回值。
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
