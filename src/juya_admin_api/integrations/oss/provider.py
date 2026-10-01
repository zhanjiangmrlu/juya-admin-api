from dataclasses import dataclass, field
from typing import Protocol

from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    upload_url: str
    object_key_prefix: str
    max_bytes: int
    expires_in: int
    fields: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    object_key: str
    size: int
    content_type: str
    sha256: str
    metadata: dict[str, str]


class OssProvider(Protocol):
    async def freeze_bytes(self, data: bytes, asset_type: str, content_type: str) -> str: ...
    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy: ...

    async def head_object(self, object_key: str) -> ObjectMetadata: ...

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes: ...

    async def sign_get_url(self, object_key: str, expires_in: int) -> str: ...

    async def delete_object(self, object_key: str) -> None: ...


def validate_object_key(key: str) -> None:
    prefixes = (
        "uploads/images/",
        "uploads/audio/",
        "feedback/",
        "generated/audio/",
        "oss-live-tests/",
        "sealed/media/",
    )
    if (
        not 1 <= len(key) <= 512
        or not key.startswith(prefixes)
        or any(part in {".", ".."} for part in key.split("/"))
        or any(ord(character) < 32 for character in key)
        or any(value in key for value in ("\\", "?", "#", "%", ":", "$"))
    ):
        raise AppError("OSS_OBJECT_KEY_INVALID", "OSS对象键不在授权范围", 422)
