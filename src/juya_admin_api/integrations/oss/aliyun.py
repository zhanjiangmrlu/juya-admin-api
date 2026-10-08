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
    def get_credentials(self) -> OssCredentials:
        # 功能: 获取当前可用于云服务调用的访问凭据.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 包含访问密钥,可选安全令牌及过期时间的 OSS 凭据.
        ...


class AliyunOssProvider:
    # 匿名函数: 为业务服务提供可注入的 UTC 当前时钟.
    # 参数: 无.
    # 返回: 当前带 UTC 时区的日期时间.
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
        # 功能: 初始化OSS 对象存储对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     region: 云服务地域标识,例如 cn-shanghai.
        #     bucket: OSS 存储桶名称.
        #     endpoint: OSS HTTPS 服务端点;None 时按地域生成默认端点.
        #     credentials_provider: 提供 OSS 服务凭据的对象.
        #     credentials_expires_at: OSS 临时凭据的过期时间,带时区.
        #     clock: 返回当前带时区时间的回调,便于控制签名和业务时间.
        # 返回: 无返回值;正常完成表示本次操作成功.
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
        # 功能: 生成限制对象目录,MIME 类型,大小和时效的 OSS 直传策略.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key_prefix: 浏览器直传允许使用的 OSS 对象键前缀.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        #     expires_in: 上传策略或下载签名的期望有效时长,单位为秒,最大 600 秒.
        # 返回: 上传地址,对象前缀,大小限制,有效期和签名表单字段.
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
        # 功能: 同步生成 OSS V4 签名的浏览器上传表单和策略.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key_prefix: 浏览器直传允许使用的 OSS 对象键前缀.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        #     expires_in: 上传策略或下载签名的期望有效时长,单位为秒,最大 600 秒.
        # 返回: 上传地址,对象前缀,大小限制,有效期和签名表单字段.
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
        # 功能: 读取 OSS 对象大小,类型和摘要元数据.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 对象的大小,MIME 类型,SHA-256 摘要及元数据.
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
        # 功能: 签发受凭据剩余寿命限制的私有 OSS 下载地址.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     expires_in: 上传策略或下载签名的期望有效时长,单位为秒,最大 600 秒.
        # 返回: 带访问签名和有效期的私有 OSS 下载地址.
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
        # 功能: 删除允许清理的 OSS 对象并保护固定教学素材.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        # 返回: 无返回值;正常完成表示本次操作成功.
        self._validate_key(object_key)
        if object_key.startswith("sealed/media/"):
            raise AppError("MEDIA_FIXED_OBJECT_PROTECTED", "固定教学素材不可删除", 409)
        await self._call(
            self._client.delete_object,
            oss.DeleteObjectRequest(bucket=self._bucket, key=object_key),
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        # 功能: 在大小限制内读取 OSS 对象的原始字节.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        # 返回: 加密内容或读取到的原始字节.
        self._validate_key(object_key)
        if not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise AppError("MEDIA_SIZE_INVALID", "读取素材大小限制无效", 422)
        return cast(bytes, await self._call(self._read_bytes_sync, object_key, max_bytes))

    def _read_bytes_sync(self, object_key: str, max_bytes: int) -> bytes:
        # 功能: 流式读取 OSS 对象并拒绝空对象或超限对象.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
        #     max_bytes: 素材允许的最大字节数;OSS 读取上限不超过 50 MiB.
        # 返回: 加密内容或读取到的原始字节.
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
        # 功能: 将素材写入不可覆盖的固定私有对象并保留版本定位符.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     data: 待校验,读取或固定存储的素材原始字节.
        #     asset_type: 固定素材类别,只接受 images 或 audio.
        #     content_type: 素材的 MIME 类型,例如 image/png 或 audio/mpeg.
        # 返回: 固定媒体对象键,启用版本控制时附带编码后的版本号.
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
        # 功能: 拆解固定素材定位符中的对象键和编码版本号.
        # 参数:
        #     locator: OSS 对象定位符,可携带 ~v~ 编码的固定版本信息.
        # 返回: OSS 对象键及可选的固定版本标识.
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
        # 功能: 构造当前 OSS 存储桶的 HTTPS 上传地址.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 当前存储桶的 HTTPS 上传地址.
        endpoint = urlsplit(self._endpoint)
        scheme = endpoint.scheme or "https"
        host = endpoint.netloc or endpoint.path
        return urlunsplit((scheme, f"{self._bucket}.{host}", "", "", ""))

    def _safe_ttl(self, credentials: OssCredentials, ttl: int, now: datetime) -> int:
        # 功能: 按凭据剩余寿命和安全余量限制签名有效秒数.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     credentials: 当前 OSS 凭据,包含签名密钥及可选过期时间.
        #     ttl: 希望使用的签名有效时长,单位为秒,需受凭据剩余寿命限制.
        #     now: 本次操作的当前时间,供有效期判定,业务记录和审计使用.
        # 返回: 不超过 600 秒且预留凭据过期安全余量的有效秒数.
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
        # 功能: 校验 OSS 对象键,拒绝越界路径或非法字符.
        # 参数:
        #     key: 待校验的 OSS 桶内对象键或固定版本定位符.
        # 返回: 无返回值;正常完成表示本次操作成功.
        validate_object_key(key)

    @staticmethod
    def _mime_types(prefix: str) -> list[str]:
        # 功能: 按上传目录选择允许的图片或音频 MIME 类型.
        # 参数:
        #     prefix: 素材对象键前缀,用于选择允许上传的 MIME 类型.
        # 返回: 上传目录允许使用的 MIME 类型列表.
        if prefix.startswith(("uploads/images/", "feedback/", "oss-live-tests/")):
            return ["image/jpeg", "image/png", "image/webp", "image/bmp"]
        return ["audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/aac"]

    async def _call(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # 功能: 在线程中执行 OSS SDK 方法并转换云服务异常.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     method: 待执行的云服务 SDK 可调用方法.
        #     *args: 传给底层 OSS SDK 方法的额外位置实参.
        #     **kwargs: 传给底层 OSS SDK 方法的额外关键字实参.
        # 返回: 指定 OSS 方法的原始响应对象;方法异常转换为携带业务码的 AppError.
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
