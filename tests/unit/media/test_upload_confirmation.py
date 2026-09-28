from datetime import UTC, datetime

import pytest

from juya_admin_api.integrations.oss.provider import ObjectMetadata, UploadPolicy
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


class FakeOss:
    def __init__(self) -> None:
        self.metadata: dict[str, ObjectMetadata] = {}

    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy:
        return UploadPolicy("https://upload.example", object_key_prefix, max_bytes, expires_in, {})

    async def head_object(self, object_key: str) -> ObjectMetadata:
        return self.metadata[object_key]

    async def sign_get_url(self, object_key: str, expires_in: int) -> str:
        return f"https://oss.example/{object_key}?ttl={expires_in}"

    async def delete_object(self, object_key: str) -> None:
        self.metadata.pop(object_key, None)


@pytest.mark.asyncio
async def test_upload_policy_and_confirmation_validate_prefix_metadata_and_deduplicate() -> None:
    oss = FakeOss()
    repository = InMemoryMediaRepository()
    service = MediaService(oss, repository)
    object_key = "uploads/images/admin-1/image.png"
    oss.metadata[object_key] = ObjectMetadata(
        object_key,
        1024,
        "image/png",
        "a" * 64,
        {"decodable": "true", "security_status": "PASSED"},
    )

    policy = await service.create_upload_policy("images", "admin-1")
    first = await service.confirm_upload("images", "admin-1", object_key, NOW)
    duplicate = await service.confirm_upload("images", "admin-1", object_key, NOW)

    assert policy.object_key_prefix == "uploads/images/admin-1/"
    assert first.id == duplicate.id
    assert len(repository.assets) == 1

    with pytest.raises(AppError) as wrong_prefix:
        await service.confirm_upload("images", "admin-1", "uploads/images/other/x.png", NOW)
    assert wrong_prefix.value.code == "MEDIA_OBJECT_KEY_INVALID"


@pytest.mark.asyncio
async def test_confirmation_rejects_type_size_decode_security_and_batch_limits() -> None:
    oss = FakeOss()
    service = MediaService(oss, InMemoryMediaRepository())
    prefix = "uploads/images/admin-1/"
    cases = {
        "mime.png": ObjectMetadata(prefix + "mime.png", 10, "text/plain", "1" * 64, {}),
        "large.png": ObjectMetadata(
            prefix + "large.png", 21 * 1024 * 1024, "image/png", "2" * 64, {}
        ),
        "decode.png": ObjectMetadata(
            prefix + "decode.png", 10, "image/png", "3" * 64, {"decodable": "false"}
        ),
        "blocked.png": ObjectMetadata(
            prefix + "blocked.png",
            10,
            "image/png",
            "4" * 64,
            {"decodable": "true", "security_status": "BLOCKED"},
        ),
    }
    for name, metadata in cases.items():
        oss.metadata[metadata.object_key] = metadata
        with pytest.raises(AppError):
            await service.confirm_upload("images", "admin-1", prefix + name, NOW)

    with pytest.raises(AppError) as image_batch:
        service.validate_batch("images", 31)
    assert image_batch.value.code == "MEDIA_BATCH_LIMIT"
    service.validate_batch("audio", 300)
    with pytest.raises(AppError):
        service.validate_batch("audio", 301)
