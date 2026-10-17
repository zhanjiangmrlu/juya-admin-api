"""Short lived administrator previews of validated archive media, with byte identity checks."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import text

from juya_admin_api.modules.content.production_store import decode
from juya_admin_api.modules.content_import.parser import SpoolFiles, read_container
from juya_admin_api.modules.content_import.service import ImportService
from juya_admin_api.modules.media.inspection import inspect_media
from juya_admin_api.modules.v15_access.credentials import CredentialCodec
from juya_admin_api.shared.errors import AppError


class PreviewCredential:
    def __init__(self, secret: bytes):
        self.codec = CredentialCodec(secret)

    def issue(self, actor: str, batch: str, path: str, sha256: str, now: datetime) -> str:
        return self.codec.sign(
            {
                "purpose": "ADMIN_IMPORT_PREVIEW",
                "actor": actor,
                "batch": batch,
                "path": path,
                "sha256": sha256,
                "expires_at": (now + timedelta(seconds=60)).isoformat(),
            }
        )

    def read(self, token: str, actor: str, batch: str, now: datetime) -> dict[str, Any]:
        try:
            claims = self.codec.verify(token, now)
            if (claims.get("purpose"), claims.get("actor"), claims.get("batch")) != (
                "ADMIN_IMPORT_PREVIEW",
                actor,
                batch,
            ):
                raise AppError("IMPORT_PREVIEW_FORBIDDEN", "预览凭据不属于当前管理员和批次", 403)
            return claims
        except AppError as error:
            if error.code == "CONFIRMATION_CREDENTIAL_INVALID":
                raise AppError("IMPORT_PREVIEW_EXPIRED", "素材预览凭据无效或已过期", 403) from error
            raise


class ImportPreviewStore:
    def __init__(self, imports: ImportService, secret: bytes, ffprobe_path: str):
        self.imports = imports
        self.credentials = PreviewCredential(secret)
        self.ffprobe_path = ffprobe_path

    async def load(self, batch_id: str, path: str) -> tuple[bytes, Any, str]:
        async with self.imports.sessions() as session:
            batch = await self.imports.batch(session, batch_id)
            if batch["status"] in {"PENDING", "FAILED", "CANCELLED"}:
                raise AppError("IMPORT_PREVIEW_NOT_READY", "批次未校验或已取消", 409)
            references = {}
            if batch["import_mode"] == "MATERIAL_ZIP":
                references = {
                    resource["path"]: resource["sha256"]
                    for resource in decode(batch["manifest"]).get("resources", [])
                }
            elif batch["import_mode"] == "ZIP":
                rows: Any = (
                    (
                        await session.execute(
                            text("SELECT candidate FROM content_import_item WHERE batch_id=:batch"),
                            {"batch": batch_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    item = decode(row)
                    for role, resource in item.get("resources", {}).items():
                        if role != "document_path":
                            references[resource["path"]] = resource["sha256"]
                    for entry in item.get("entries", []):
                        for field in ("image_path", "pronunciation_path"):
                            if entry.get(field) and entry.get(field + "_hash"):
                                references[entry[field]] = entry[field + "_hash"]
            if path not in references:
                raise AppError("IMPORT_RESOURCE_NOT_FOUND", "路径不属于本批次校验过的素材", 404)
        extension = PurePosixPath(path).suffix.lower()
        if extension in {".png", ".jpg", ".jpeg", ".webp"}:
            kind = "images"
        elif extension in {".mp3", ".wav", ".m4a", ".aac"}:
            kind = "audio"
        else:
            raise AppError("IMPORT_PREVIEW_TYPE_INVALID", "只支持实际图片或音频素材预览", 422)
        files = await asyncio.to_thread(
            read_container, await self.imports.original(batch), self.imports.limits, spool=True
        )
        try:
            data = files[path]
        finally:
            if isinstance(files, SpoolFiles):
                files.close()
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 != references[path]:
            raise AppError("IMPORT_ARTIFACT_CHANGED", "素材字节已变化", 409)
        inspected = await inspect_media(data, kind, self.ffprobe_path)
        if kind == "images" and inspected.content_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            raise AppError("IMPORT_PREVIEW_TYPE_INVALID", "实际图片格式不可预览", 422)
        return data, inspected, sha256

    async def issue(self, batch_id: str, path: str, actor: str) -> dict[str, Any]:
        _data, inspected, sha256 = await self.load(batch_id, path)
        now = datetime.now(UTC)
        token = self.credentials.issue(actor, batch_id, path, sha256, now)
        return {
            "url": "/api/v1/admin/content-imports/"
            + batch_id
            + "/resources/preview-content?"
            + urlencode({"token": token}),
            "expires_at": (now + timedelta(seconds=60)).isoformat(),
            "sha256": sha256,
            "content_type": inspected.content_type,
            "size_bytes": inspected.size,
            "duration_ms": inspected.duration_ms,
            "review_status": "ADMIN_PREVIEW_ONLY",
        }

    async def content(self, batch_id: str, token: str, actor: str) -> tuple[bytes, str]:
        claims = self.credentials.read(token, actor, batch_id, datetime.now(UTC))
        data, inspected, sha256 = await self.load(batch_id, claims["path"])
        self.credentials.read(token, actor, batch_id, datetime.now(UTC))
        if sha256 != claims["sha256"]:
            raise AppError("IMPORT_ARTIFACT_CHANGED", "预览素材字节已变化", 409)
        return data, inspected.content_type
