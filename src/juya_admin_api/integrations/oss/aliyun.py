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
from juya_admin_api.integrations.oss.provider import (
    ObjectMetadata,
    UploadPolicy,
    validate_object_key,
)
from juya_admin_api.shared.errors import AppError
from juya_admin_api.shared.ids import new_ulid


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
        if not object_key_prefix.startswith(
            ("uploads/images/", "uploads/audio/", "feedback/", "oss-live-tests/")
        ):
            raise AppError("OSS_OBJECT_KEY_INVALID", "固定素材目录不允许浏览器上传", 422)
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
                {"x-oss-meta-security_status": "PENDING"},
                {"x-oss-meta-decodable": "false"},
                {"x-oss-forbid-overwrite": "true"},
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
            "x-oss-meta-security_status": "PENDING",
            "x-oss-meta-decodable": "false",
            "x-oss-forbid-overwrite": "true",
        }
        security_token = getattr(credentials, "security_token", None)
        if security_token:
            fields["x-oss-security-token"] = security_token
        return UploadPolicy(self._bucket_url(), object_key_prefix, max_bytes, expires_in, fields)

    async def head_object(self, object_key: str) -> ObjectMetadata:
        self._validate_key(object_key)
        key, version = self._locator(object_key)
        result = await self._call(
            self._client.head_object,
            oss.HeadObjectRequest(bucket=self._bucket, key=key, version_id=version),
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
        key, version = self._locator(object_key)
        expires_in = self._safe_ttl(
            self._credentials_provider.get_credentials(), expires_in, self._clock()
        )
        result = await self._call(
            self._client.presign,
            oss.GetObjectRequest(bucket=self._bucket, key=key, version_id=version),
            expires=timedelta(seconds=expires_in),
        )
        return str(result.url)

    async def delete_object(self, object_key: str) -> None:
        self._validate_key(object_key)
        if object_key.startswith("sealed/media/"):
            raise AppError("MEDIA_FIXED_OBJECT_PROTECTED", "固定教学素材不可删除", 409)
        await self._call(
            self._client.delete_object,
            oss.DeleteObjectRequest(bucket=self._bucket, key=object_key),
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        self._validate_key(object_key)
        if not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise AppError("MEDIA_SIZE_INVALID", "读取素材大小限制无效", 422)
        return cast(bytes, await self._call(self._read_bytes_sync, object_key, max_bytes))

    def _read_bytes_sync(self, object_key: str, max_bytes: int) -> bytes:
        key, version = self._locator(object_key)
        result = self._client.get_object(
            oss.GetObjectRequest(bucket=self._bucket, key=key, version_id=version)
        )
        try:
            if int(result.content_length or 0) > max_bytes:
                raise AppError("MEDIA_SIZE_INVALID", "素材大小超限", 422)
            data = bytearray()
            for chunk in result.body.iter_bytes(chunk_size=64 * 1024):
                if len(data) + len(chunk) > max_bytes:
                    raise AppError("MEDIA_SIZE_INVALID", "素材大小超限", 422)
                data.extend(chunk)
            if not data:
                raise AppError("MEDIA_SIZE_INVALID", "素材大小不符合要求", 422)
            return bytes(data)
        finally:
            result.body.close()

    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str:
        if asset_type not in {"images", "audio"} or not data or len(data) > 50 * 1024 * 1024:
            raise AppError("MEDIA_SIZE_INVALID", "固定素材参数无效", 422)
        state = await self._call(
            self._client.get_bucket_versioning, oss.GetBucketVersioningRequest(bucket=self._bucket)
        )
        status = state.version_status or ""
        if status not in {"", "Enabled"}:
            raise AppError(
                "OSS_VERSIONING_UNSAFE", "Bucket版本状态无法固定素材, 请检查版本控制", 409
            )
        suffix = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/bmp": "bmp",
            "audio/wav": "wav",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/aac": "aac",
        }.get(content_type)
        if suffix is None:
            raise AppError("MEDIA_MIME_INVALID", "固定素材格式无效", 422)
        key = f"sealed/media/{asset_type}/{new_ulid(datetime.now(UTC))}.{suffix}"
        result = await self._call(
            self._client.put_object,
            oss.PutObjectRequest(
                bucket=self._bucket,
                key=key,
                body=data,
                content_type=content_type,
                acl="private",
                content_md5=base64.b64encode(
                    hashlib.md5(data, usedforsecurity=False).digest()
                ).decode(),
                forbid_overwrite="true",
                metadata={"sha256": hashlib.sha256(data).hexdigest()},
            ),
        )
        version = getattr(result, "version_id", None)
        if version == "null":
            raise AppError(
                "OSS_VERSIONING_UNSAFE", "OSS返回可覆盖的空版本, 请检查Bucket版本状态", 503
            )
        if status == "Enabled" and (
            not isinstance(version, str) or not version or version == "null"
        ):
            raise AppError("OSS_VERSIONING_UNSAFE", "OSS未返回固定对象版本", 503)
        if isinstance(version, str) and version and version != "null":
            key += "~v~" + base64.urlsafe_b64encode(version.encode()).decode().rstrip("=")
        self._validate_key(key)
        return key

    @staticmethod
    def _locator(locator: str) -> tuple[str, str | None]:
        if "~v~" not in locator:
            return locator, None
        key, encoded = locator.rsplit("~v~", 1)
        if not key.startswith("sealed/media/") or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,512}", encoded
        ):
            raise AppError("OSS_OBJECT_KEY_INVALID", "固定素材版本无效", 422)
        try:
            version = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (ValueError, UnicodeError):
            raise AppError("OSS_OBJECT_KEY_INVALID", "固定素材版本无效", 422) from None
        if not version or version == "null":
            raise AppError("OSS_OBJECT_KEY_INVALID", "固定素材版本无效", 422)
        return key, version

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
        validate_object_key(key)

    @staticmethod
    def _mime_types(prefix: str) -> list[str]:
        if prefix.startswith(("uploads/images/", "feedback/", "oss-live-tests/")):
            return ["image/jpeg", "image/png", "image/webp", "image/bmp"]
        return ["audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/aac"]

    async def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except AppError:
            raise
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
