from datetime import UTC, datetime, timedelta

import pytest

from juya_admin_api.modules.content_import.preview import PreviewCredential
from juya_admin_api.shared.errors import AppError


def test_preview_is_bound_to_actor_batch_path_bytes_and_sixty_second_window():
    codec = PreviewCredential(b"test-preview-signing-secret")
    now = datetime.now(UTC)
    token = codec.issue("admin", "batch", "images/a.png", "a" * 64, now)
    assert codec.read(token, "admin", "batch", now)["sha256"] == "a" * 64
    for actor, batch, clock in (
        ("another", "batch", now),
        ("admin", "other", now),
        ("admin", "batch", now + timedelta(seconds=60)),
    ):
        with pytest.raises(AppError):
            codec.read(token, actor, batch, clock)
    with pytest.raises(AppError):
        codec.read(token[:-1] + ("0" if token[-1] != "0" else "1"), "admin", "batch", now)
