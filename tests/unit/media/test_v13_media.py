import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from juya_admin_api.integrations.content_security.protocol import SecurityResult
from juya_admin_api.integrations.oss.provider import ObjectMetadata
from juya_admin_api.modules.media.service import InMemoryMediaRepository, MediaService
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 1, tzinfo=UTC)
KEY = "uploads/images/admin-1/a.png"


def png_bytes(width: int = 32, height: int = 24) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (width, height), "blue").save(stream, "PNG")
    return stream.getvalue()


class BytesOss:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def head_object(self, object_key: str) -> ObjectMetadata:
        return ObjectMetadata(
            object_key,
            len(self.data),
            "image/png",
            "a" * 64,
            {"decodable": "true", "security_status": "PASSED"},
        )

    async def read_bytes(self, object_key: str, max_bytes: int) -> bytes:
        assert len(self.data) <= max_bytes
        return self.data


class Security:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = 0

    async def scan_image(self, object_key: str) -> SecurityResult:
        self.calls += 1
        return SecurityResult("trusted-scan", self.status)


@pytest.mark.asyncio
async def test_confirmation_decodes_server_bytes_and_ignores_client_hash_and_gate() -> None:
    data = png_bytes()
    security = Security("PASSED")
    service = MediaService(BytesOss(data), InMemoryMediaRepository(), security=security)
    asset = await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert asset.sha256 == hashlib.sha256(data).hexdigest()
    assert (asset.width, asset.height, asset.duration_ms) == (32, 24, None)
    assert security.calls == 1


@pytest.mark.asyncio
async def test_forged_safe_metadata_cannot_confirm_undecodable_or_blocked_file() -> None:
    for data, status, code in [
        (b"not a png", "PASSED", "MEDIA_DECODE_FAILED"),
        (png_bytes(), "BLOCKED", "MEDIA_SECURITY_BLOCKED"),
    ]:
        service = MediaService(BytesOss(data), InMemoryMediaRepository(), security=Security(status))
        with pytest.raises(AppError) as error:
            await service.confirm_upload("images", "admin-1", KEY, NOW)
        assert error.value.code == code


@pytest.mark.asyncio
async def test_unconfigured_security_fails_closed_even_with_valid_image() -> None:
    service = MediaService(BytesOss(png_bytes()), InMemoryMediaRepository())
    with pytest.raises(AppError) as error:
        await service.confirm_upload("images", "admin-1", KEY, NOW)
    assert error.value.code == "MEDIA_SECURITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_quota_reservation_is_atomic_and_idempotent_and_requires_month_verification() -> None:
    from juya_admin_api.modules.media.quota import InMemoryOcrQuotaRepository, OcrQuotaService

    quota = OcrQuotaService(InMemoryOcrQuotaRepository())
    with pytest.raises(AppError) as disabled:
        await quota.reserve("job-1", NOW)
    assert disabled.value.code == "OCR_DISABLED"
    await quota.configure(
        enabled=True,
        monthly_limit=1,
        free_quota=1,
        paid_disabled=True,
        verify_quota=True,
        actor_id="admin-1",
        now=NOW,
    )
    outcomes = await asyncio.gather(
        quota.reserve("job-1", NOW), quota.reserve("job-2", NOW), return_exceptions=True
    )
    assert sum(isinstance(item, AppError) for item in outcomes) == 1
    winner = "job-1" if not isinstance(outcomes[0], AppError) else "job-2"
    await quota.reserve(winner, NOW)
    assert (await quota.status(NOW))["reserved_count"] == 1
    with pytest.raises(AppError) as expired:
        await quota.reserve("job-next", datetime(2026, 11, 1, tzinfo=UTC))
    assert expired.value.code == "OCR_QUOTA_UNVERIFIED"


@pytest.mark.asyncio
async def test_pending_audio_confirmation_reuses_security_task_and_blocks_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from juya_admin_api.modules.media.inspection import InspectedMedia

    class AudioOss(BytesOss):
        async def head_object(self, object_key: str) -> ObjectMetadata:
            return ObjectMetadata(object_key, 16, "audio/wav", "", {})

    class AsyncSecurity:
        submitted = 0
        queried = 0

        async def scan_audio(self, object_key: str) -> SecurityResult:
            self.submitted += 1
            return SecurityResult("audio-task-1", "PENDING")

        async def poll_audio(self, request_id: str) -> SecurityResult:
            assert request_id == "audio-task-1"
            self.queried += 1
            return SecurityResult(request_id, "PASSED")

    async def inspect(data: bytes, kind: str, path: str) -> InspectedMedia:
        return InspectedMedia("audio/wav", 16, "f" * 64, duration_ms=1000)

    monkeypatch.setattr("juya_admin_api.modules.media.service.inspect_media", inspect)
    security = AsyncSecurity()
    service = MediaService(AudioOss(b"fixture"), InMemoryMediaRepository(), security=security)
    key = "uploads/audio/admin-1/fixtures/a.wav"
    pending = await service.confirm_upload("audio", "admin-1", key, NOW)
    assert pending.status == pending.security_status == "PENDING"
    with pytest.raises(AppError):
        await service.get_asset(pending.id)
    confirmed = await service.confirm_upload("audio", "admin-1", key, NOW)
    assert confirmed.id == pending.id and confirmed.status == "CONFIRMED"
    assert (security.submitted, security.queried) == (1, 1)


@pytest.mark.asyncio
async def test_legacy_unbound_target_signature_fails_closed() -> None:
    from juya_admin_api.modules.media.repository import SQLAlchemySignedTargetResolver

    resolver = SQLAlchemySignedTargetResolver(None, None)
    with pytest.raises(AppError) as error:
        await resolver("unbound-target", "user", NOW)
    assert error.value.code == "SCENE_RESOURCE_BINDING_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("frames,stderr", [("0", b""), ("1", b"invalid audio frame")])
async def test_audio_inspection_requires_decoded_frames_and_no_decoder_errors(
    monkeypatch: pytest.MonkeyPatch, frames: str, stderr: bytes
) -> None:
    from juya_admin_api.modules.media.inspection import inspect_media

    def probe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert "-count_frames" in args
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "format": {"duration": "1.000", "format_name": "wav"},
                    "streams": [{"codec_type": "audio", "nb_read_frames": frames}],
                }
            ).encode(),
            stderr,
        )

    monkeypatch.setattr("juya_admin_api.modules.media.inspection.subprocess.run", probe)
    with pytest.raises(AppError) as error:
        await inspect_media(b"header without valid frames", "audio")
    assert error.value.code == "MEDIA_DECODE_FAILED"
