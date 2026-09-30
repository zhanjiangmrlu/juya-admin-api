from dataclasses import dataclass, field
from typing import Protocol


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
    async def create_upload_policy(
        self, object_key_prefix: str, max_bytes: int, expires_in: int
    ) -> UploadPolicy: ...

    async def head_object(self, object_key: str) -> ObjectMetadata: ...

    async def sign_get_url(self, object_key: str, expires_in: int) -> str: ...

    async def delete_object(self, object_key: str) -> None: ...
