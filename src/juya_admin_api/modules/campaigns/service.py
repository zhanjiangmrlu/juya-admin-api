import hashlib
import json
from datetime import datetime
from typing import Any, Protocol

from fastapi.encoders import jsonable_encoder

from juya_admin_api.modules.campaigns.domain import CampaignDuration, CampaignVersion
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.idempotency import IdempotencyService


class CampaignRepository(Protocol):
    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]: ...

    async def get(self, campaign_id: str) -> dict[str, Any] | None: ...

    async def save(
        self,
        campaign_id: str | None,
        *,
        expected_version: int | None = None,
        name: str,
        now: datetime,
        duration_days: int | None = None,
        activation_window_days: int | None = None,
        capacity: int | None = None,
        scene_ids: tuple[str, ...] | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]: ...

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        now: datetime,
        capacity: int | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]: ...


class CampaignService:
    def __init__(self, repository: CampaignRepository, idempotency: IdempotencyService) -> None:
        self._repository = repository
        self._idempotency = idempotency

    async def list(self, filters: dict[str, str], page: int, page_size: int) -> dict[str, Any]:
        return await self._repository.list(filters, page, page_size)

    async def get(self, campaign_id: str) -> dict[str, Any]:
        result = await self._repository.get(campaign_id)
        if result is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "活动不存在", 404)
        return result

    async def save(
        self,
        campaign_id: str | None,
        *,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        **fields: Any,
    ) -> dict[str, Any]:
        request = {"campaign_id": campaign_id, **fields}
        record = await self._idempotency.begin(
            "campaign.save", actor_id, idempotency_key, _hash(request)
        )
        if record.is_replay:
            assert record.response_body is not None
            return record.response_body
        try:
            result = await self._repository.save(campaign_id, actor_id=actor_id, now=now, **fields)
            body: dict[str, Any] = jsonable_encoder(result)
            await self._idempotency.complete(record, body)
            return body
        except Exception:
            await self._idempotency.abort(record)
            raise

    async def command(
        self,
        campaign_id: str,
        operation: str,
        *,
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
        now: datetime,
        capacity: int | None = None,
    ) -> dict[str, Any]:
        request = {
            "campaign_id": campaign_id,
            "operation": operation,
            "expected_version": expected_version,
            "capacity": capacity,
        }
        record = await self._idempotency.begin(
            "campaign.command", actor_id, idempotency_key, _hash(request)
        )
        if record.is_replay:
            assert record.response_body is not None
            return record.response_body
        try:
            result = await self._repository.command(
                campaign_id,
                operation,
                expected_version=expected_version,
                actor_id=actor_id,
                now=now,
                capacity=capacity,
            )
            body: dict[str, Any] = jsonable_encoder(result)
            await self._idempotency.complete(record, body)
            return body
        except Exception:
            await self._idempotency.abort(record)
            raise

    @staticmethod
    def revise_version(
        current: CampaignVersion,
        *,
        duration_days: CampaignDuration,
        activation_window_days: int,
        capacity: int,
        scene_ids: tuple[str, ...],
    ) -> CampaignVersion:
        if current.locked_at is not None and (
            duration_days != current.duration_days
            or activation_window_days != current.activation_window_days
            or scene_ids != current.scene_ids
        ):
            raise AppError(
                "CAMPAIGN_VERSION_LOCKED",
                "活动首次开通后时长、启动窗口和场景不可修改",
                409,
            )
        current.duration_days = duration_days
        current.activation_window_days = activation_window_days
        current.capacity = capacity
        current.scene_ids = scene_ids
        current.validate()
        return current


def _hash(request: dict[str, Any]) -> str:
    payload = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
