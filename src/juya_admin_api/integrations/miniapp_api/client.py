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
    verified_by_name: str | None = None


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
    # 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
    # 参数: 无.
    # 返回: 当前带 UTC 时区的日期时间.
    # 匿名函数: 为内部签名请求生成一次性随机数.
    # 参数: 无.
    # 返回: 16 个随机字节编码的 32 字符十六进制字符串.
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
        # 功能: 初始化小程序内部 API对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     base_url: 小程序内部 API 的服务根地址.
        #     secret: 内部服务共享签名密钥,签名时使用其原始字节.
        #     service_name: 内部调用方或当前服务的名称.
        #     http: 可注入的异步 HTTP 客户端;None 时由服务创建并负责关闭.
        #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
        #     nonce_factory: 生成内部签名请求随机数的回调.
        # 返回: 无返回值;正常完成表示本次操作成功.
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
        # 功能: 关闭由内部 API 客户端创建的 HTTP 连接资源.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if self._owns_http:
            await self._http.aclose()

    async def search_user_ids_by_wechat(
        self, wechat_id: str, admin_id: str = "system"
    ) -> tuple[str, ...]:
        # 功能: 通过微信号查询匹配用户的公开标识集合.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     wechat_id: 用户提交的微信号,用于精确检索关联用户.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 匹配条件的用户公开标识集合.
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
        # 功能: 批量查询用户联系信息投影及可用状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_ids: 用户公开标识集合,限制批量投影查询或搜索范围.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 联系人投影集合及上游可用状态.
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
        # 功能: 查询指定用户的学习概览.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 指定用户的学习概览.
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
        # 功能: 分页查询联系信息纠错申请.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     status: 联系人跟进状态或纠错处理状态.
        #     page: 分页页码,从 1 开始,默认第 1 页.
        #     page_size: 每页返回条数,接口范围为 1 至 100,默认 20.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 纠错申请列表及分页信息.
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
        # 功能: 获取指定联系信息纠错申请详情.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 联系信息纠错申请详情.
        payload = await self._request_json(
            "GET",
            f"/internal/v1/contact-corrections/{quote(correction_id, safe='')}",
            extra_headers={"X-Admin-Id": admin_id},
        )
        return _contact_correction(payload)

    async def update_contact_status(
        self, user_id: str, status: str, admin_id: str
    ) -> ContactProjection:
        # 功能: 更新用户联系信息的跟进状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     status: 联系人跟进状态或纠错处理状态.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 用户的联系信息投影.
        payload = await self._request_json(
            "POST",
            f"/internal/v1/users/{quote(user_id, safe='')}/contact-status",
            {"status": status},
            extra_headers={"X-Admin-Id": admin_id},
        )
        return _contact_from_user_payload(payload, user_id)

    async def verify_contact_change(self, user_id: str, admin_id: str) -> ContactProjection:
        # 功能: 确认用户联系信息变更已经核实.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     admin_id: 执行本次操作的管理员公开标识.
        # 返回: 用户的联系信息投影.
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
        # 功能: 提交联系信息纠错处理决定并保留幂等语义.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     correction_id: 联系人纠错申请公开标识.
        #     decision: 纠错处理决定,例如接受或拒绝.
        #     admin_id: 执行本次操作的管理员公开标识.
        #     idempotency_key: 本次业务写操作的幂等键,相同主体和作用域内重试应使用同一个键.
        # 返回: 纠错处理后的结果.
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
        # 功能: 向接收用户的内部消息接口提交幂等站内通知
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     payload: 发件箱通知字段,接收用户用于路径,其余字段用于消息请求体
        #     event_id: 跨服务事件唯一标识,写入请求体以防止重试重复创建消息
        # 返回: 无返回值,正常完成表示本次操作成功
        user_id = _string_field(payload, "user_id")
        message = {key: value for key, value in payload.items() if key != "user_id"}
        message["event_id"] = event_id
        await self._request_json(
            "POST",
            f"/internal/v1/users/{quote(user_id, safe='')}/messages",
            message,
            extra_headers={"X-Idempotency-Key": event_id},
        )

    async def record_deletion_cleanup_result(
        self, user_id: str, deletion_request_id: str, event_id: str
    ) -> None:
        # 功能: 向小程序服务回报用户注销关联数据清理完成.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     deletion_request_id: 小程序注销请求标识,用于关联清理结果与注销流程.
        #     event_id: 跨服务事件的唯一标识,防止重复投递或重复清理.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self._request_json(
            "POST",
            f"/internal/v1/users/{quote(user_id, safe='')}/deletion-cleanup-result",
            {"deletion_request_id": deletion_request_id, "succeeded": True},
            extra_headers={"X-Event-Id": event_id},
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        # 功能: 签名并发送内部 HTTP 请求,统一校验 JSON 响应及错误.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     method: 内部 HTTP 请求方法,例如 GET 或 POST.
        #     path: 小程序内部 API 的相对请求路径.
        #     payload: 内部请求的 JSON 数据;None 表示不发送请求体.
        #     extra_headers: 内部请求额外请求头,例如管理员身份和幂等键.
        # 返回: 已验证为字典的上游 JSON 响应;业务字段由所调用的内部端点决定.
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
        # 功能: 将上游非成功响应转换为稳定的业务异常.
        # 参数:
        #     response: 小程序内部 API 返回的 HTTP 响应.
        # 返回: 无返回值;正常完成表示本次操作成功.
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
    # 功能: 构造上游响应结构不可信的业务异常.
    # 参数: 无.
    # 返回: 包含业务码,说明及 HTTP 状态的异常对象.
    return AppError("MINIAPP_API_INVALID_RESPONSE", "用户服务响应无效", 502)


def _mapping(value: object) -> Mapping[str, object]:
    # 功能: 校验上游返回内容为映射结构.
    # 参数:
    #     value: 待确认结构的上游 JSON 响应内容.
    # 返回: 通过结构校验的上游响应映射.
    if not isinstance(value, Mapping):
        raise _invalid_response()
    return value


def _list_field(payload: Mapping[str, object], name: str) -> list[object]:
    # 功能: 读取并校验上游响应中的列表字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 通过结构校验的上游列表字段.
    value = payload.get(name)
    if not isinstance(value, list):
        raise _invalid_response()
    return value


def _string_field(payload: Mapping[str, object], name: str) -> str:
    # 功能: 读取并校验上游响应中的必填非空字符串字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 指定字段的非空字符串;缺失,空串或类型错误时抛出上游响应错误.
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise _invalid_response()
    return value


def _nullable_string_field(payload: Mapping[str, object], name: str) -> str | None:
    # 功能: 读取并校验上游响应中的可空字符串字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 处理后的字符串;无匹配内容或输入允许为空时为 None.
    if name not in payload:
        raise _invalid_response()
    value = payload[name]
    if value is not None and not isinstance(value, str):
        raise _invalid_response()
    return value


def _bool_field(payload: Mapping[str, object], name: str) -> bool:
    # 功能: 读取并校验上游响应中的布尔字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 条件成立或操作成功时为 True,否则为 False.
    value = payload.get(name)
    if not isinstance(value, bool):
        raise _invalid_response()
    return value


def _nonnegative_int(payload: Mapping[str, object], name: str) -> int:
    # 功能: 读取并校验上游响应中的非负整数.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 指定字段的非负整数;布尔值,负数或其他类型触发上游响应错误.
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid_response()
    return value


def _positive_int(payload: Mapping[str, object], name: str) -> int:
    # 功能: 读取并校验上游响应中的正整数.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 指定字段的大于零整数;布尔值,零,负数或其他类型触发上游响应错误.
    value = _nonnegative_int(payload, name)
    if value < 1:
        raise _invalid_response()
    return value


def _datetime_field(
    payload: Mapping[str, object], name: str, *, nullable: bool = False
) -> datetime | None:
    # 功能: 解析并校验上游响应中的日期时间字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    #     nullable: 是否接受字段为 None;False 时缺失或空值视为无效响应.
    # 返回: 规范化日期时间;输入为空或允许空值时为 None.
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
    # 功能: 读取不可为空的上游日期时间字段.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     name: 从上游 JSON 响应读取并校验的字段名.
    # 返回: 规范化或计算后的带时区日期时间.
    value = _datetime_field(payload, name)
    if value is None:
        raise _invalid_response()
    return value


def _contact_projection(
    payload: Mapping[str, object], *, user_id: str | None = None
) -> ContactProjection:
    # 功能: 把可信响应映射转换为联系人投影.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
    # 返回: 用户的联系信息投影.
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
    # 功能: 从用户响应中提取联系信息投影并绑定用户标识.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
    # 返回: 用户的联系信息投影.
    contact = payload.get("contact")
    return _contact_projection(_mapping(contact), user_id=user_id)


def _contact_correction(payload: Mapping[str, object]) -> ContactCorrection:
    # 功能: 把可信响应映射转换为联系信息纠错申请.
    # 参数:
    #     payload: 已验证为映射结构的小程序内部 API 响应,从中读取业务字段.
    # 返回: 联系信息纠错申请详情.
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
