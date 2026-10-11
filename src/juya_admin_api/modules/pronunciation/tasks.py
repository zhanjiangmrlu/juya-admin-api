import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from juya_admin_api.integrations.content_security.protocol import ContentSecurityProvider
from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.pronunciation.domain import (
    AudioResult,
    Config,
    SynthesisFailure,
    Target,
)
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.shared.errors import AppError


class SynthesisProvider(Protocol):
    async def generate(self, text: str, config: Config, session_id: str) -> AudioResult: ...


class PronunciationWorker:
    def __init__(
        self,
        repository: SqlPronunciationRepository,
        provider: SynthesisProvider,
        oss: OssProvider,
        security: ContentSecurityProvider,
        ffprobe_path: str = "ffprobe",
        current_config: Callable[[], Config] | None = None,
    ):
        self.repository = repository
        self.provider = provider
        self.oss = oss
        self.security = security
        self.ffprobe_path = ffprobe_path
        self.current_config = current_config

    async def run(self, task_id: str) -> None:
        now = int(time.time())
        job = await self.repository.claim(task_id, now)
        if job is None:
            return
        target = await self.repository.target(job["target_id"])
        if target is None or target.status != "CONFIRMED" or not target.allow_synthesis:
            await self.repository.fenced_update(
                job, status="FAILED", error_code="TARGET_DISABLED", lease_until=0, lease_token=None
            )
            return
        config = Config(**job["config"])
        target = Target(**job["target_snapshot"])
        attempt_id = None
        try:
            audio = job["audio"]
            if audio is None:
                if self.current_config is not None and not self.current_config().enabled:
                    raise AppError("TTS_DISABLED", "合成配置已关闭", 409)
                if not config.enabled or config.price_version == "unconfigured":
                    raise AppError("TTS_DISABLED", "合成配置不可用", 409)
                period = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
                attempt_id = await self.repository.reserve(
                    job, period, config, len(target.confirmed_text), now
                )
                result = await self.provider.generate(target.confirmed_text, config, attempt_id)
                audio = result.audio
                # Persist paid result before inspection/upload so retries only repeat local stages.
                await self.repository.fenced_update(job, audio=audio, request_id=result.request_id)
                await self.repository.finish_attempt(
                    attempt_id,
                    status="SUCCEEDED",
                    provider_request_id=result.request_id,
                    actual_characters=result.billed_characters,
                )
            inspected = await inspect_media(audio, "audio", self.ffprobe_path)
            object_key = job.get("object_key")
            if object_key is None:
                object_key = await self.oss.freeze_bytes(audio, "audio", inspected.content_type)
                await self.repository.fenced_update(job, object_key=object_key)
            scan = await self.security.scan_audio(object_key)
            if scan.status in ("PENDING", "PROCESSING"):
                scan = await self.security.poll_audio(scan.provider_request_id)
            if scan.status != "PASSED":
                if scan.status in ("PENDING", "PROCESSING"):
                    raise AppError("AUDIO_SECURITY_PENDING", "音频审核尚未完成", 409)
                raise AppError("AUDIO_SECURITY_REJECTED", "音频未通过安全审核", 422)
            await self.repository.complete(
                job,
                object_key,
                inspected.duration_ms or 0,
                sha256=inspected.sha256,
                size_bytes=inspected.size,
                content_type=inspected.content_type,
                security_status=scan.status,
                security_request_id=scan.provider_request_id,
            )
        except SynthesisFailure as exc:
            if attempt_id:
                await self.repository.finish_attempt(
                    attempt_id,
                    status="UNKNOWN" if exc.unknown else "FAILED",
                    error_code=exc.code,
                    provider_request_id=exc.request_id,
                )
            retry = exc.retryable and not exc.unknown and job["attempt"] < 2
            await self.repository.fenced_update(
                job,
                status="UNKNOWN" if exc.unknown else ("RETRY" if retry else "FAILED"),
                error_code=exc.code,
                next_retry_at=now + 2 ** (job["attempt"] + 1) * 30,
                lease_until=0,
                lease_token=None,
                outbox_pending=retry,
            )
        except Exception as exc:
            current = await self.repository.task(task_id)
            if isinstance(exc, AppError) and exc.code == "LEASE_LOST":
                return
            paid = current is not None and current["audio"] is not None
            code = exc.code if isinstance(exc, AppError) else "LOCAL_STAGE_FAILED"
            rejected = code == "AUDIO_SECURITY_REJECTED"
            if attempt_id and not paid:
                await self.repository.finish_attempt(attempt_id, status="UNKNOWN", error_code=code)
            await self.repository.fenced_update(
                job,
                status=("FAILED" if rejected else "RECOVER")
                if paid
                else ("UNKNOWN" if attempt_id else "FAILED"),
                error_code=code,
                next_retry_at=now + 60,
                lease_until=0,
                lease_token=None,
                outbox_pending=paid and not rejected,
            )


async def dispatch_pending(
    repository: SqlPronunciationRepository, dispatch: Callable[[str], Awaitable[None]]
) -> int:
    pending = await repository.pending(int(time.time()))
    for task_id in pending:
        await dispatch(task_id)
    return len(pending)
