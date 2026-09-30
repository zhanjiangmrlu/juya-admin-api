import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import alibabacloud_oss_v2 as oss  # type: ignore[import-untyped]

from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider
from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.shared.errors import AppError


class OssCredentials(Protocol):
    access_key_id: str
    access_key_secret: str
    security_token: str | None


class CredentialsProvider(Protocol):
    def get_credentials(self) -> OssCredentials: ...


class AliyunOssProvider:
    def __init__(
        self,
        region: str,
        bucket: str,
        *,
        endpoint: str | None = None,
        credentials_provider: CredentialsProvider | None = None,
        credentials_expires_at: datetime | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        provider = credentials_provider or ControlledCredentialsProvider()
        config = oss.config.load_default()
        config.credentials_provider = provider
        config.region = region
        if endpoint:
            config.endpoint = endpoint
        self._client = oss.Client(config)
        self._credentials_provider = cast(CredentialsProvider, provider)
        self._region = region
        self._bucket = bucket
        self._endpoint = endpoint or f"https://oss-{region}.aliyuncs.com"
        self._credentials_expires_at = credentials_expires_at
        self._clock = clock
        endpoint_parts = urlsplit(
            self._endpoint if "://" in self._endpoint else "https://" + self._endpoint
        )
        if (
            endpoint_parts.scheme != "https"
            or endpoint_parts.path not in {"", "/"}
            or endpoint_parts.query
            or endpoint_parts.fragment
        ):
            raise RuntimeError("OSS endpoint must be an HTTPS service endpoint")

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        self._validate_key(object_key_prefix)
        if max_bytes < 1 or not 1 <= expires_in <= 600:
            raise AppError("OSS_POLICY_INVALID", "OSS上传策略限制无效", 422)
        return await asyncio.to_thread(
            self._create_upload_policy_sync, object_key_prefix, max_bytes, expires_in
        )

    def _create_upload_policy_sync(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        credentials = self._credentials_provider.get_credentials()
        now = self._clock()
        expires_in = self._safe_ttl(credentials, expires_in, now)
        date = now.strftime("%Y%m%d")
        product = "oss"
        credential = (
            f"{credentials.access_key_id}/{date}/{self._region}/{product}/aliyun_v4_request"
        )
        policy_data: dict[str, Any] = {
            "expiration": (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "conditions": [
                {"bucket": self._bucket},
                {"x-oss-signature-version": "OSS4-HMAC-SHA256"},
                {"x-oss-credential": credential},
                {"x-oss-date": now.strftime("%Y%m%dT%H%M%SZ")},
                ["starts-with", "$key", object_key_prefix],
                ["content-length-range", 1, max_bytes],
                ["in", "$Content-Type", self._mime_types(object_key_prefix)],
            ],
        }
        security_token = getattr(credentials, "security_token", None)
        if security_token:
            policy_data["conditions"].append({"x-oss-security-token": security_token})
        policy = base64.b64encode(json.dumps(policy_data, separators=(",", ":")).encode()).decode()
        signing_key = f"aliyun_v4{credentials.access_key_secret}".encode()
        for value in (date, self._region, product, "aliyun_v4_request"):
            signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
        signature = hmac.new(signing_key, policy.encode(), hashlib.sha256).hexdigest()
        fields = {
            "key": f"{object_key_prefix}${{filename}}",
            "policy": policy,
            "x-oss-signature-version": "OSS4-HMAC-SHA256",
            "x-oss-credential": credential,
            "x-oss-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "x-oss-signature": signature,
            "Content-Type": self._mime_types(object_key_prefix)[0],
        }
        security_token = getattr(credentials, "security_token", None)
        if security_token:
            fields["x-oss-security-token"] = security_token
        return UploadPolicy(self._bucket_url(), object_key_prefix, max_bytes, expires_in, fields)

    async def head_object(self, object_key: str) -> ObjectMetadata:
        self._validate_key(object_key)
        result = await self._call(
            self._client.head_object,
            oss.HeadObjectRequest(bucket=self._bucket, key=object_key),
        )
        metadata = {str(key).lower(): str(value) for key, value in (result.metadata or {}).items()}
        return ObjectMetadata(
            object_key=object_key,
            size=int(result.content_length or 0),
            content_type=result.content_type or "application/octet-stream",
            sha256=metadata.get("sha256", ""),
            metadata=metadata,
        )

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        self._validate_key(object_key)
        expires_in = self._safe_ttl(
            self._credentials_provider.get_credentials(), expires_in, self._clock()
        )
        result = await self._call(
            self._client.presign,
            oss.GetObjectRequest(bucket=self._bucket, key=object_key),
            expires=timedelta(seconds=expires_in),
        )
        return str(result.url)

    async def delete_object(self, object_key: str) -> None:
        self._validate_key(object_key)
        await self._call(
            self._client.delete_object,
            oss.DeleteObjectRequest(bucket=self._bucket, key=object_key),
        )

    def _bucket_url(self) -> str:
        endpoint = urlsplit(self._endpoint)
        scheme = endpoint.scheme or "https"
        host = endpoint.netloc or endpoint.path
        return urlunsplit((scheme, f"{self._bucket}.{host}", "", "", ""))

    def _safe_ttl(self, credentials: OssCredentials, ttl: int, now: datetime) -> int:
        expiry = getattr(credentials, "expiration", None) or self._credentials_expires_at
        if expiry is not None:
            if expiry.tzinfo is None:
                raise AppError("OSS_CREDENTIALS_EXPIRED", "OSS临时凭据过期或时间无效", 503)
            ttl = min(ttl, int((expiry - now).total_seconds()) - 30)
        if ttl <= 0 or ttl > 600:
            raise AppError("OSS_CREDENTIALS_EXPIRED", "OSS临时凭据过期或有效期无效", 503)
        return ttl

    @staticmethod
    def _validate_key(key: str) -> None:
        prefixes = (
            "uploads/images/",
            "uploads/audio/",
            "feedback/",
            "generated/audio/",
            "oss-live-tests/",
        )
        if (
            not key.startswith(prefixes)
            or any(part in {".", ".."} for part in key.split("/"))
            or any(value in key for value in ("\\", "?", "#", "%", "\x00"))
        ):
            raise AppError("OSS_OBJECT_KEY_INVALID", "OSS对象键不在授权范围", 422)

    @staticmethod
    def _mime_types(prefix: str) -> list[str]:
        if prefix.startswith(("uploads/images/", "feedback/", "oss-live-tests/")):
            return ["image/jpeg", "image/png", "image/webp"]
        return ["audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/aac"]

    async def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except Exception as error:
            for _ in range(5):
                unwrap = getattr(error, "unwrap", None)
                if not callable(unwrap):
                    break
                error = unwrap()
            codes = {
                "SecurityTokenExpired": "OSS_CREDENTIALS_EXPIRED",
                "InvalidSecurityToken": "OSS_CREDENTIALS_INVALID",
                "RequestTimeTooSkewed": "OSS_CLOCK_SKEW",
                "SignatureDoesNotMatch": "OSS_SIGNATURE_INVALID",
                "AccessDenied": "OSS_ACCESS_DENIED",
                "NoSuchKey": "OSS_OBJECT_NOT_FOUND",
            }
            code = codes.get(str(getattr(error, "code", "")), "OSS_OPERATION_FAILED")
            request_id = getattr(error, "request_id", None)
            details = (
                {"oss_request_id": request_id}
                if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", request_id)
                else {}
            )
            raise AppError(code, "OSS操作失败: 请检查权限、凭据或服务时间", 503, details) from None
