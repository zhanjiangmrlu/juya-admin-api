from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from juya_admin_api.integrations.oss.provider import ObjectMetadata
from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key
from juya_admin_api.modules.v15_access.ports import ResourceRequest
from juya_admin_api.modules.v15_access.resources import SQLResourceSigner
from juya_admin_api.shared.errors import AppError

NOW = datetime(2026, 10, 11, tzinfo=UTC)


def asset():
    return dict(
        public_id="asset",
        object_key="sealed/media/audio/file.mp3",
        asset_type="audio",
        content_type="audio/mpeg",
        size_bytes=100,
        sha256="a" * 64,
        status="CONFIRMED",
        security_status="PASSED",
        duration_ms=1000,
        width=None,
        height=None,
    )


def request(target="pron", version="version"):
    return ResourceRequest(
        "user", "dialogue", "scene", "revision", target, version, "WORD", favorite_id="favorite"
    )


class Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class Session:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.queries = []

    async def execute(self, statement, params=None):
        self.queries.append(str(statement))
        return Result(next(self.rows))

    async def scalar(self, statement, params=None):
        self.queries.append(str(statement))
        return next(self.rows)


@pytest.mark.asyncio
async def test_actual_metadata_and_time_bound_are_verified_before_signing():
    provider = AsyncMock()
    provider.head_object.return_value = ObjectMetadata(
        "sealed/media/audio/file.mp3", 100, "audio/mpeg", "a" * 64, {}
    )
    provider.sign_get_url.return_value = "https://private/audio?Expires=" + str(
        int((NOW + timedelta(seconds=10)).timestamp())
    )
    times = iter([NOW, NOW + timedelta(seconds=3), NOW + timedelta(seconds=3)])
    signer = SQLResourceSigner(provider, clock=lambda: next(times))
    signer.binding = AsyncMock(return_value=(["pron"], None))
    signer.audio = AsyncMock(return_value=asset())
    result = await signer(Session([]), request(), NOW + timedelta(seconds=10))
    assert result["expires_at"] == (NOW + timedelta(seconds=10)).isoformat()
    provider.sign_get_url.assert_awaited_once_with("sealed/media/audio/file.mp3", 7)
    provider.head_object.return_value = ObjectMetadata(
        "sealed/media/audio/file.mp3", 99, "audio/mpeg", "a" * 64, {}
    )
    signer.clock = lambda: NOW
    with pytest.raises(AppError):
        await signer(Session([]), request(), NOW + timedelta(seconds=60))
    assert provider.sign_get_url.await_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "READY"),
        ("security_status", "BLOCKED"),
        ("duration_ms", 0),
        ("object_key", "uploads/audio/mutable.mp3"),
        ("sha256", "bad"),
        ("size_bytes", 0),
        ("asset_type", "images"),
    ],
)
def test_unconfirmed_unsafe_mutable_or_invalid_actual_media_is_rejected(field, value):
    signer = SQLResourceSigner(AsyncMock())
    with pytest.raises(AppError):
        signer.validate_asset({**asset(), field: value}, "audio")


@pytest.mark.asyncio
async def test_uploaded_pronunciation_uses_the_real_fixed_audio_target_and_version():
    signer = SQLResourceSigner(AsyncMock())
    target = asdict(
        Target("pron", "medium", uploaded_target_id="uploaded", uploaded_resource_version="v1")
    )
    version = dict(status="SUPERSEDED", target_id="uploaded", target_type="WORD", asset_id="asset")
    session = Session([target, version, asset()])
    assert (await signer.audio(session, request("pron", "v1"), ["pron"]))["public_id"] == "asset"
    assert all("FOR SHARE" in query for query in session.queries)
    with pytest.raises(AppError):
        await signer.audio(Session([target, version]), request("unrelated", "v1"), ["pron"])
    with pytest.raises(AppError):
        await signer.audio(
            Session([target, {**version, "target_type": "DIALOGUE"}]),
            request("pron", "v1"),
            ["pron"],
        )


@pytest.mark.asyncio
async def test_cache_resource_must_match_confirmed_reading_and_frozen_config():
    signer = SQLResourceSigner(AsyncMock())
    target = Target("pron", "medium")
    config = Config()
    cached = dict(
        cache_key=cache_key(target, config),
        object_key=asset()["object_key"],
        duration_ms=1000,
        resource_version="job",
        status="READY",
        config=asdict(config),
        target_id="pron",
        target_snapshot=asdict(target),
    )
    session = Session([asdict(target), None, cached, "asset", asset()])
    assert (await signer.audio(session, request("pron", "job"), ["pron"]))["duration_ms"] == 1000
    with pytest.raises(AppError):
        await signer.audio(
            Session([asdict(Target("pron", "different reading")), None, cached]),
            request("pron", "job"),
            ["pron"],
        )
    with pytest.raises(AppError):
        await signer.audio(
            Session([asdict(target), None, {**cached, "status": "RUNNING"}]),
            request("pron", "job"),
            ["pron"],
        )


def test_provider_cannot_extend_real_signature_beyond_authorized_expiry():
    with pytest.raises(AppError):
        SQLResourceSigner.signature_expiry(
            "https://private?Expires=" + str(int((NOW + timedelta(seconds=61)).timestamp())),
            NOW + timedelta(seconds=60),
        )
    assert SQLResourceSigner.signature_expiry(
        "https://private?Expires=" + str(int((NOW + timedelta(seconds=20)).timestamp())),
        NOW + timedelta(seconds=60),
    ) == NOW + timedelta(seconds=20)
