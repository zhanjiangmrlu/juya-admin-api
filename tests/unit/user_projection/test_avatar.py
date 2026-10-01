from unittest.mock import AsyncMock

import pytest

from juya_admin_api.modules.user_projection.avatar import profile_avatar_url


@pytest.mark.asyncio
async def test_avatar_signing_never_signs_other_users_or_private_media() -> None:
    sign = AsyncMock(return_value="https://example.test/avatar")
    for key in ("media/private.png", "avatars/other/photo.png", "avatars/user/../private.png"):
        assert await profile_avatar_url("user", key, sign) is None
    sign.assert_not_called()
    assert (
        await profile_avatar_url("user", "avatars/user/photo.png", sign)
        == "https://example.test/avatar"
    )
    sign.assert_awaited_once_with("avatars/user/photo.png", 300)
