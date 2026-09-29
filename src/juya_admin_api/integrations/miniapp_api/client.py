import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from juya_admin_api.infrastructure.security.service_hmac import sign_request
from juya_admin_api.shared.errors import AppError

_SAFE_UPSTREAM_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


@dataclass(frozen=True, slots=True)
class ContactProjection:
    user_id: str
    wechat_id: str | None
    contact_status: str
    change_pending: bool
    verified_at: datetime | None
    verified_by: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContactProjectionResult:
    contacts: tuple[ContactProjection, ...]
    degraded: bool


@dataclass(frozen=True, slots=True)
class LearningOverview:
    open_scene_completed_count: int
    learning_days: int
    favorite_count: int


@dataclass(frozen=True, slots=True)
class ContactTimelineEvent:
    status: str
    actor_type: str
    actor_id: str
    event_type: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ContactCorrection:
    id: str
    user_id: str
    juya_number: str
    nickname: str | None
    wechat_id: str | None
    reason: str
    status: str
    created_at: datetime
    processed_at: datetime | None
    timeline: tuple[ContactTimelineEvent, ...]


@dataclass(frozen=True, slots=True)
class ContactCorrectionPage:
    items: tuple[ContactCorrection, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class CorrectionDecision:
    id: str
    status: str
    processed_at: datetime


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

    async def search_user_ids_by_wechat(
        self, wechat_id: str, admin_id: str = "system"
    ) -> tuple[str, ...]:
        payload = await self._request_json(
            "POST",
            "/internal/v1/users/search",
            {"wechat_id": wechat_id},
            extra_headers={"X-Admin-Id": admin_id},
        )
        user_ids: list[str] = []
        for item in _list_field(payload, "items"):
            mapping = _mapping(item)
            user_ids.append(_string_field(mapping, "public_id"))
        return tuple(user_ids)

    async def get_contact_projections(
        self, user_ids: tuple[str, ...], admin_id: str = "system"
    ) -> ContactProjectionResult:
        try:
            payload = await self._request_json(
                "POST",
                "/internal/v1/users/contact-projections",
                {"user_ids": list(user_ids)},
                extra_headers={"X-Admin-Id": admin_id},
            )
        except AppError as error:
            if error.code == "MINIAPP_API_UNAVAILABLE":
                return ContactProjectionResult((), True)
            raise
        contacts = tuple(
            _contact_projection(_mapping(item)) for item in _list_field(payload, "contacts")
        )
        return ContactProjectionResult(contacts, False)

    async def get_learning_overview(self, user_id: str, admin_id: str) -> LearningOverview:
        payload = await self._request_json(
            "GET",
            f"/internal/v1/users/{quote(user_id, safe='')}/learning-overview",
            extra_headers={"X-Admin-Id": admin_id},
        )
        return LearningOverview(
            open_scene_completed_count=_nonnegative_int(payload, "open_scene_completed_count"),
            learning_days=_nonnegative_int(payload, "learning_days"),
            favorite_count=_nonnegative_int(payload, "favorite_count"),
        )

    async def list_contact_corrections(
        self, status: str | None, page: int, page_size: int, admin_id: str
    ) -> ContactCorrectionPage:
        payload = await self._request_json(
            "POST",
            "/internal/v1/contact-corrections/search",
            {"status": status, "page": page, "page_size": page_size},
            extra_headers={"X-Admin-Id": admin_id},
        )
        return ContactCorrectionPage(
            items=tuple(
                _contact_correction(_mapping(item)) for item in _list_field(payload, "items")
            ),
            total=_nonnegative_int(payload, "total"),
            page=_positive_int(payload, "page"),
            page_size=_positive_int(payload, "page_size"),
        )

    async def get_contact_correction(self, correction_id: str, admin_id: str) -> ContactCorrection:
        payload = await self._request_json(
            "GET",
            f"/internal/v1/contact-corrections/{quote(correction_id, safe='')}",
            extra_headers={"X-Admin-Id": admin_id},
        )
        return _contact_correction(payload)

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection:
        payload = await self._request_json(
            "POST",
            f"/internal/v1/users/{quote(user_id, safe='')}/contact-status",
            {"status": status},
            extra_headers={"X-Admin-Id": admin_id},
        )
        return _contact_from_user_payload(payload, user_id)

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
        payload = await self._request_json(
            "POST",
            f"/internal/v1/users/{quote(user_id, safe='')}/contact/verify-change",
            {},
            extra_headers={"X-Admin-Id": admin_id},
        )
        return _contact_from_user_payload(payload, user_id)

    async def decide_contact_correction(
        self,
        correction_id: str,
        decision: str,
        admin_id: str,
        idempotency_key: str,
    ) -> CorrectionDecision:
        payload = await self._request_json(
            "POST",
            f"/internal/v1/contact-corrections/{quote(correction_id, safe='')}/decision",
            {"decision": decision},
            extra_headers={
                "X-Admin-Id": admin_id,
                "X-Idempotency-Key": idempotency_key,
            },
        )
        return CorrectionDecision(
            id=_string_field(payload, "id"),
            status=_string_field(payload, "status"),
            processed_at=_required_datetime(payload, "processed_at"),
        )

    async def create_message(self, payload: dict[str, object], event_id: str) -> None:
        await self._request_json(
            "POST",
            "/internal/v1/messages",
            payload,
            extra_headers={"X-Idempotency-Key": event_id},
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        normalized_method = method.upper()
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        timestamp = int(self._clock().timestamp())
        nonce = self._nonce_factory()
        headers = {
            "X-Juya-Service": self._service_name,
            "X-Juya-Timestamp": str(timestamp),
            "X-Juya-Nonce": nonce,
            "X-Juya-Signature": sign_request(
                normalized_method, path, timestamp, nonce, body, self._secret
            ),
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        try:
            response = await self._http.request(
                normalized_method,
                f"{self._base_url}{path}",
                content=body,
                headers=headers,
            )
        except httpx.TransportError as error:
            raise AppError("MINIAPP_API_UNAVAILABLE", "用户服务暂时不可用", 503) from error
        if response.status_code >= 400:
            self._raise_upstream_error(response)
        try:
            data = response.json()
        except ValueError as error:
            raise _invalid_response() from error
        if not isinstance(data, dict):
            raise _invalid_response()
        return data

    @staticmethod
    def _raise_upstream_error(response: httpx.Response) -> None:
        if response.status_code in {404, 409, 422}:
            try:
                body = response.json()
            except ValueError:
                body = None
            code = body.get("code") if isinstance(body, Mapping) else None
            if isinstance(code, str) and _SAFE_UPSTREAM_ERROR.fullmatch(code):
                raise AppError(
                    code,
                    "用户服务请求未完成",
                    response.status_code,
                    {"upstream_status": response.status_code},
                )
        raise AppError(
            "MINIAPP_API_UNAVAILABLE",
            "用户服务暂时不可用",
            503,
            {"upstream_status": response.status_code},
        )


def _invalid_response() -> AppError:
    return AppError("MINIAPP_API_INVALID_RESPONSE", "用户服务响应无效", 502)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid_response()
    return value


def _list_field(payload: Mapping[str, object], name: str) -> list[object]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise _invalid_response()
    return value


def _string_field(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise _invalid_response()
    return value


def _nullable_string_field(payload: Mapping[str, object], name: str) -> str | None:
    if name not in payload:
        raise _invalid_response()
    value = payload[name]
    if value is not None and not isinstance(value, str):
        raise _invalid_response()
    return value


def _bool_field(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise _invalid_response()
    return value


def _nonnegative_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid_response()
    return value


def _positive_int(payload: Mapping[str, object], name: str) -> int:
    value = _nonnegative_int(payload, name)
    if value < 1:
        raise _invalid_response()
    return value


def _datetime_field(
    payload: Mapping[str, object], name: str, *, nullable: bool = False
) -> datetime | None:
    if name not in payload:
        raise _invalid_response()
    value = payload[name]
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _invalid_response()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _invalid_response() from error
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _required_datetime(payload: Mapping[str, object], name: str) -> datetime:
    value = _datetime_field(payload, name)
    if value is None:
        raise _invalid_response()
    return value


def _contact_projection(
    payload: Mapping[str, object], *, user_id: str | None = None
) -> ContactProjection:
    resolved_user_id = user_id or _string_field(payload, "user_id")
    updated_at = _required_datetime(payload, "updated_at")
    return ContactProjection(
        user_id=resolved_user_id,
        wechat_id=_nullable_string_field(payload, "wechat_id"),
        contact_status=_string_field(payload, "contact_status"),
        change_pending=_bool_field(payload, "change_pending"),
        verified_at=_datetime_field(payload, "verified_at", nullable=True),
        verified_by=_nullable_string_field(payload, "verified_by"),
        updated_at=updated_at,
    )


def _contact_from_user_payload(payload: Mapping[str, object], user_id: str) -> ContactProjection:
    contact = payload.get("contact")
    return _contact_projection(_mapping(contact), user_id=user_id)


def _contact_correction(payload: Mapping[str, object]) -> ContactCorrection:
    timeline = tuple(
        ContactTimelineEvent(
            status=_string_field(mapping, "status"),
            actor_type=_string_field(mapping, "actor_type"),
            actor_id=_string_field(mapping, "actor_id"),
            event_type=_string_field(mapping, "event_type"),
            occurred_at=_required_datetime(mapping, "occurred_at"),
        )
        for mapping in (_mapping(item) for item in _list_field(payload, "timeline"))
    )
    created_at = _required_datetime(payload, "created_at")
    return ContactCorrection(
        id=_string_field(payload, "id"),
        user_id=_string_field(payload, "user_id"),
        juya_number=_string_field(payload, "juya_number"),
        nickname=_nullable_string_field(payload, "nickname"),
        wechat_id=_nullable_string_field(payload, "wechat_id"),
        reason=_string_field(payload, "reason"),
        status=_string_field(payload, "status"),
        created_at=created_at,
        processed_at=_datetime_field(payload, "processed_at", nullable=True),
        timeline=timeline,
    )
