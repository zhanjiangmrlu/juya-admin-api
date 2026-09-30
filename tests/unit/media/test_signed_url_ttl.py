from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class SigningOss:
    def __init__(self) -> None:
        self.ttls: list[int] = []

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        raise NotImplementedError

    async def head_object(self, object_key: str) -> ObjectMetadata:
        raise NotImplementedError

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        self.ttls.append(expires_in)
        return f"https://oss.example/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_signed_url_ttl_is_truncated_to_entitlement_expiry() -> None:
    oss = SigningOss()
    service = MediaService(oss, InMemoryMediaRepository(), signed_url_ttl_seconds=300)

    signed = await service.sign_media("audio/target-1.mp3", NOW + timedelta(seconds=91), NOW)

    assert signed.expires_at == NOW + timedelta(seconds=91)
    assert oss.ttls == [91]


@pytest.mark.asyncio
async def test_signed_url_rejects_zero_or_expired_ttl() -> None:
    oss = SigningOss()
    service = MediaService(oss, InMemoryMediaRepository())

    with pytest.raises(AppError) as expired:
        await service.sign_media("audio/target-1.mp3", NOW, NOW)
    assert expired.value.code == "MEDIA_ACCESS_EXPIRED"
    assert oss.ttls == []


@pytest.mark.asyncio
async def test_reported_expiry_does_not_outlive_sts_capped_oss_signature() -> None:
    class StsSigningOss(SigningOss):
        async def sign_get_url(self, object_key: str, expires_in: int) -> str:
            return "https://oss.example/a?x-oss-date=20260929T000000Z&x-oss-expires=60&x-oss-signature=private"

    service = MediaService(StsSigningOss(), InMemoryMediaRepository())
    signed = await service.sign_media("feedback/user/a.png", None, NOW)
    assert signed.expires_at == NOW + timedelta(seconds=60)
    assert "private" not in repr(signed)
