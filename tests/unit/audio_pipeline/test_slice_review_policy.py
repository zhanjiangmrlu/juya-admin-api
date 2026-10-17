from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from juya_admin_api.infrastructure import v15_runtime
from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.modules.media.service import MediaAsset
from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("review_enabled", "review_status", "allowed"),
    [(False, "SKIPPED", True), (True, "SKIPPED", False), (False, "BLOCKED", False)],
)
async def test_slice_preparation_preserves_actual_review_status(
    monkeypatch, review_enabled, review_status, allowed
):
    asset = MediaAsset(
        "slice-asset",
        "sealed/media/audio/slice.wav",
        "audio",
        "audio/wav",
        100,
        "a" * 64,
        "READY",
        "PENDING",
        "admin",
        datetime.now(UTC),
        duration_ms=500,
    )
    repository = SimpleNamespace(get=AsyncMock(return_value=asset), update_security=AsyncMock())
    monkeypatch.setattr(v15_runtime, "SQLAlchemyMediaRepository", lambda sessions: repository)
    monkeypatch.setattr(
        v15_runtime,
        "inspect_media",
        AsyncMock(
            return_value=SimpleNamespace(
                sha256=asset.sha256,
                duration_ms=asset.duration_ms,
            )
        ),
    )
    settings = Settings(
        _env_file=None,
        content_security_enabled=review_enabled,
        internal_hmac_secret="isolated-policy-test-secret-32bytes",
    )
    safety = SimpleNamespace(
        scan_audio=AsyncMock(
            return_value=SimpleNamespace(
                status=review_status,
                provider_request_id="review-fixture",
            )
        ),
    )
    sessions = MagicMock()
    session = MagicMock()
    session.execute = AsyncMock()
    sessions.return_value.__aenter__.return_value = session
    oss = SimpleNamespace(read_bytes=AsyncMock(return_value=b"policy-fixture"))
    runtime = v15_runtime.build_v15_services(settings, sessions, oss, MagicMock(), safety)
    try:
        assert runtime.authoring.require_review == review_enabled
        if allowed:
            await runtime.authoring.prepare_slice(asset.id, "slice-version")
        else:
            with pytest.raises(AppError) as error:
                await runtime.authoring.prepare_slice(asset.id, "slice-version")
            assert error.value.code == "SLICE_REVIEW_PENDING"
        updated = repository.update_security.call_args.args[0]
        assert updated.security_status == review_status
        assert updated.status == ("CONFIRMED" if allowed else "PENDING")
    finally:
        await runtime.client.aclose()
