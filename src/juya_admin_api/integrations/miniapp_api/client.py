import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from juya_admin_api.infrastructure.security.service_hmac import sign_request
from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class ContactProjection:
    user_id: str
    wechat_id: str | None
    contact_status: str


@dataclass(frozen=True, slots=True)
class ContactProjectionResult:
    contacts: tuple[ContactProjection, ...]
    degraded: bool


class MiniappApiClient:
    def __init__(
        self,
        base_url: str,
        secret: bytes,
        *,
        service_name: str = "juya-admin-api",
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._service_name = service_name
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
            base_url=self._base_url,
        )
        self._owns_http = http is None
        self._clock = clock
        self._nonce_factory = nonce_factory

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def search_user_ids_by_wechat(self, wechat_id: str) -> tuple[str, ...]:
        payload = await self._post("/internal/v1/users/search-by-wechat", {"wechat_id": wechat_id})
        return tuple(str(value) for value in _list_field(payload, "user_ids"))

    async def get_contact_projections(self, user_ids: tuple[str, ...]) -> ContactProjectionResult:
        try:
            payload = await self._post(
                "/internal/v1/users/contact-projections", {"user_ids": list(user_ids)}
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return ContactProjectionResult((), True)
        contacts_list: list[ContactProjection] = []
        for item in _list_field(payload, "contacts"):
            if not isinstance(item, Mapping):
                raise AppError("MINIAPP_API_INVALID_RESPONSE", "用户服务响应无效", 502)
            contacts_list.append(
                ContactProjection(
                    user_id=str(item["user_id"]),
                    wechat_id=(None if item.get("wechat_id") is None else str(item["wechat_id"])),
                    contact_status=str(item["contact_status"]),
                )
            )
        contacts = tuple(contacts_list)
        return ContactProjectionResult(contacts, False)

    async def create_message(self, payload: dict[str, object], event_id: str) -> None:
        await self._post(
            "/internal/v1/messages",
            payload,
            extra_headers={"X-Idempotency-Key": event_id},
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = int(self._clock().timestamp())
        nonce = self._nonce_factory()
        headers = {
            "Content-Type": "application/json",
            "X-Juya-Service": self._service_name,
            "X-Juya-Timestamp": str(timestamp),
            "X-Juya-Nonce": nonce,
            "X-Juya-Signature": sign_request("POST", path, timestamp, nonce, body, self._secret),
        }
        headers.update(extra_headers or {})
        response = await self._http.post(f"{self._base_url}{path}", content=body, headers=headers)
        if response.status_code >= 400:
            raise AppError(
                "MINIAPP_API_UNAVAILABLE",
                "用户服务暂时不可用",
                503,
                {"upstream_status": response.status_code},
            )
        data = response.json()
        if not isinstance(data, dict):
            raise AppError("MINIAPP_API_INVALID_RESPONSE", "用户服务响应无效", 502)
        return data


def _list_field(payload: dict[str, object], name: str) -> list[object]:
    value = payload.get(name, [])
    if not isinstance(value, list):
        raise AppError("MINIAPP_API_INVALID_RESPONSE", "用户服务响应无效", 502)
    return value
