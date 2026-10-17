from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.v15_access.credentials import CredentialCodec
from juya_admin_api.shared.errors import AppError


def test_credential_signed_all_identity_fields_and_expiry():
    codec = CredentialCodec(b"a-long-private-test-secret")
    now = datetime.now(UTC)
    claims = {
        "user_id": "user",
        "entry_id": "entry",
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
    }
    token = codec.sign(claims)
    assert codec.verify(token, now) == claims
    with pytest.raises(AppError):
        codec.verify(token[:-1] + ("a" if token[-1] != "a" else "b"), now)
    with pytest.raises(AppError):
        codec.verify(token, now + timedelta(seconds=31))
