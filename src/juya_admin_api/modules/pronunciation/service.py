from collections.abc import Awaitable, Callable
from typing import Any

from juya_admin_api.modules.pronunciation.domain import Config, cache_key
from juya_admin_api.modules.pronunciation.repository import SqlPronunciationRepository
from juya_admin_api.shared.errors import AppError

Authorizer = Callable[[str, dict[str, Any]], Awaitable[bool]]
ResourceResolver = Callable[[str, str | None, dict[str, Any]], Awaitable[dict[str, Any]]]


class PronunciationService:
    def __init__(
        self,
        repository: SqlPronunciationRepository,
        config: Config,
        authorizer: Authorizer,
        resources: ResourceResolver,
    ):
        self.repository = repository
        self.config = config
        self.authorizer = authorizer
        self.resources = resources

    async def authorize(self, target_id: str, context: dict[str, Any]) -> None:
        if context.get("purpose") not in ("CONTENT", "FAVORITE_REVIEW"):
            raise AppError("PRONUNCIATION_FORBIDDEN", "发音上下文无效", 403)
        if not await self.authorizer(target_id, context):
            raise AppError("PRONUNCIATION_FORBIDDEN", "无权访问发音目标", 403)

    async def resolve(self, target_id: str, context: dict[str, Any]) -> dict[str, Any]:
        await self.authorize(target_id, context)
        target = await self.repository.target(target_id)
        if (
            target is None
            or target.status != "CONFIRMED"
            or target.target_type not in ("WORD", "CHUNK")
        ):
            raise AppError("PRONUNCIATION_UNAVAILABLE", "发音目标不可用", 404)
        if target.uploaded_target_id:
            resource = await self.resources(
                target.uploaded_target_id, target.uploaded_resource_version, context
            )
            return {"status": "READY", "source": "UPLOADED", **resource}
        key = cache_key(target, self.config)
        cached = await self.repository.cached(key)
        if cached:
            resource = await self.resources(key, cached["resource_version"], context)
            return {"status": "READY", "source": "CACHE", **resource}
        if not target.allow_synthesis or not self.config.enabled:
            raise AppError("TTS_DISABLED", "此发音目标未开放合成", 409)
        if target.language != "en" or not 0 < len(target.confirmed_text.encode("utf-8")) <= 500:
            raise AppError("TTS_TEXT_LIMIT", "请为此条目上传音频", 422)
        job = await self.repository.enqueue(target, key, self.config)
        return self.public_task(job)

    async def poll(self, task_id: str, context: dict[str, Any]) -> dict[str, Any]:
        job = await self.repository.task(task_id)
        if job is None:
            raise AppError("PRONUNCIATION_TASK_NOT_FOUND", "发音任务不存在", 404)
        requested_target_id = context.get("target_id", job["target_id"])
        await self.authorize(requested_target_id, context)
        target = await self.repository.target(requested_target_id)
        if target is None or target.status != "CONFIRMED":
            raise AppError("PRONUNCIATION_UNAVAILABLE", "发音目标不可用", 404)
        if cache_key(target, Config(**job["config"])) != job["cache_key"]:
            raise AppError("PRONUNCIATION_FORBIDDEN", "任务与发音目标不匹配", 403)
        if job["status"] == "READY":
            return {
                "status": "READY",
                "task_id": task_id,
                **await self.resources(job["cache_key"], job["public_id"], context),
            }
        return self.public_task(job)

    @staticmethod
    def public_task(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": job["public_id"],
            "status": job["status"],
            "error_code": job.get("error_code"),
            "next_retry_at": job.get("next_retry_at", 0),
        }
