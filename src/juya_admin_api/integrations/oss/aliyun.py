import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit

import alibabacloud_oss_v2 as oss  # type: ignore[import-untyped]

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy


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
    ) -> None:
        provider = credentials_provider or oss.credentials.EnvironmentVariableCredentialsProvider()
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

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        return await asyncio.to_thread(
            self._create_upload_policy_sync, object_key_prefix, max_bytes, expires_in
        )

    def _create_upload_policy_sync(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        credentials = self._credentials_provider.get_credentials()
        now = datetime.now(UTC)
        date = now.strftime("%Y%m%d")
        product = "oss"
        credential = (
            f"{credentials.access_key_id}/{date}/{self._region}/{product}/aliyun_v4_request"
        )
        policy_data = {
            "expiration": (now + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "conditions": [
                {"bucket": self._bucket},
                {"x-oss-signature-version": "OSS4-HMAC-SHA256"},
                {"x-oss-credential": credential},
                {"x-oss-date": now.strftime("%Y%m%dT%H%M%SZ")},
                ["starts-with", "$key", object_key_prefix],
                ["content-length-range", 1, max_bytes],
            ],
        }
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
        }
        security_token = getattr(credentials, "security_token", None)
        if security_token:
            fields["x-oss-security-token"] = security_token
        return UploadPolicy(self._bucket_url(), object_key_prefix, max_bytes, expires_in, fields)

    async def head_object(self, object_key: str) -> ObjectMetadata:
        result = await asyncio.to_thread(
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
        result = await asyncio.to_thread(
            self._client.presign,
            oss.GetObjectRequest(bucket=self._bucket, key=object_key),
            expires=timedelta(seconds=expires_in),
        )
        return str(result.url)

    async def delete_object(self, object_key: str) -> None:
        await asyncio.to_thread(
            self._client.delete_object,
            oss.DeleteObjectRequest(bucket=self._bucket, key=object_key),
        )

    def _bucket_url(self) -> str:
        endpoint = urlsplit(self._endpoint)
        scheme = endpoint.scheme or "https"
        host = endpoint.netloc or endpoint.path
        return urlunsplit((scheme, f"{self._bucket}.{host}", "", "", ""))
