"""Opt-in test-bucket operations. Never print credentials, policies or signed URLs."""

import base64
import hashlib
import os
from uuid import uuid4

import alibabacloud_oss_v2 as oss
import httpx
import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider

pytestmark = pytest.mark.skipif(
    os.getenv("JUYA_RUN_LIVE_OSS_TESTS", "").lower() != "true",
    reason="live OSS tests are explicitly opt-in",
)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6xkAAAAASUVORK5CYII="
)


def provider() -> AliyunOssProvider:
    settings = Settings()
    if (
        settings.environment != "test"
        or settings.oss_bucket != "juya-test"
        or settings.oss_expected_bucket != "juya-test"
    ):
        pytest.fail("Live tests require the explicitly bound test bucket", pytrace=False)
    settings.validate_oss_configuration()
    return AliyunOssProvider("cn-shenzhen", "juya-test")


@pytest.mark.asyncio
async def test_private_bucket_v4_upload_head_signed_read_and_owned_cleanup() -> None:
    storage = provider()
    prefix = f"oss-live-tests/{uuid4().hex}/"
    key = prefix + "pixel.png"
    attempted = False
    try:
        acl = await storage._call(
            storage._client.get_bucket_acl, oss.GetBucketAclRequest(bucket="juya-test")
        )
        if acl.acl != "private":
            pytest.fail("Test bucket must remain private", pytrace=False)
        policy = await storage.create_upload_policy(prefix, 1024, 120)
        fields = {
            **policy.fields,
            "key": key,
            "Content-Type": "image/png",
            "x-oss-meta-sha256": hashlib.sha256(PNG).hexdigest(),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            attempted = True
            response = await client.post(
                policy.upload_url, data=fields, files={"file": ("pixel.png", PNG, "image/png")}
            )
            if response.status_code not in {200, 204}:
                pytest.fail(f"V4 upload returned HTTP {response.status_code}", pytrace=False)
            metadata = await storage.head_object(key)
            assert metadata.size == len(PNG)
            assert metadata.content_type == "image/png"
            signed_url = await storage.sign_get_url(key, 60)
            signed_read = await client.get(signed_url)
            assert signed_read.status_code == 200
            assert signed_read.content == PNG
            anonymous_read = await client.get(policy.upload_url + "/" + key)
            assert anonymous_read.status_code == 403
    finally:
        if attempted:
            await storage.delete_object(key)
            # A signed read after deletion must prove the owned object is gone.
            async with httpx.AsyncClient(timeout=20) as client:
                gone = await client.get(await storage.sign_get_url(key, 60))
                assert gone.status_code == 404


@pytest.mark.asyncio
async def test_browser_origin_has_exact_post_get_and_head_cors() -> None:
    storage = provider()
    allowed_origin = "http://localhost:5173"
    async with httpx.AsyncClient(timeout=20) as client:
        for method in ("POST", "GET", "HEAD"):
            response = await client.options(
                storage._bucket_url() + "/oss-live-tests/cors-probe",
                headers={
                    "Origin": allowed_origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            if response.headers.get("access-control-allow-origin") != allowed_origin:
                pytest.fail(
                    f"CORS does not explicitly allow localhost:5173 for {method}", pytrace=False
                )
        denied = await client.options(
            storage._bucket_url() + "/oss-live-tests/cors-probe",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert denied.headers.get("access-control-allow-origin") is None


@pytest.mark.asyncio
async def test_policy_rejects_wrong_prefix_mime_size_and_security_verdict() -> None:
    storage = provider()
    prefix = f"oss-live-tests/{uuid4().hex}/"
    policy = await storage.create_upload_policy(prefix, 1024, 120)
    candidates = (
        (prefix + "bad-mime.png", {"Content-Type": "text/html"}, PNG),
        (prefix + "oversized.png", {"Content-Type": "image/png"}, b"x" * 1025),
        (
            prefix + "fake-verdict.png",
            {"Content-Type": "image/png", "x-oss-meta-security_status": "PASSED"},
            PNG,
        ),
        (f"oss-live-tests/{uuid4().hex}/wrong-prefix.png", {"Content-Type": "image/png"}, PNG),
    )
    async with httpx.AsyncClient(timeout=20) as client:
        for key, overrides, content in candidates:
            fields = {**policy.fields, "key": key, **overrides}
            response = await client.post(
                policy.upload_url,
                data=fields,
                files={"file": ("pixel.png", content, fields["Content-Type"])},
            )
            if response.status_code in {200, 204}:
                await storage.delete_object(key)
                pytest.fail("OSS accepted an upload outside its signed constraints", pytrace=False)
            # OSS rejects policy violations with 400 (e.g. object size) or 403.
            assert response.status_code in {400, 403}
