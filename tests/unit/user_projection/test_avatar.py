from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import alibabacloud_oss_v2 as oss
import pytest

from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.modules.user_projection.avatar import profile_avatar_url


@pytest.mark.asyncio
async def test_avatar_signing_never_signs_other_users_or_private_media() -> None:
    # 功能:验证头像签名不签其他用户或私有媒体。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    sign = AsyncMock(return_value="https://example.test/avatar")
    for key in ("media/private.png", "avatars/other/photo.png", "avatars/user/../private.png"):
        assert await profile_avatar_url("user", key, sign) is None
    sign.assert_not_called()
    assert (
        await profile_avatar_url("user", "avatars/user/photo.png", sign)
        == "https://example.test/avatar"
    )
    sign.assert_awaited_once_with("avatars/user/photo.png", 300)


@pytest.mark.asyncio
async def test_profile_avatar_signing_uses_real_oss_provider() -> None:
    # 功能: 验证真实 OSS 适配器能为用户固定头像签发五分钟读取地址
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )
    url = await profile_avatar_url("user", "avatars/user/photo.png", provider.sign_get_url)
    assert url is not None
    parsed = urlsplit(url)
    assert parsed.hostname == "juya-test.oss-cn-shenzhen.aliyuncs.com"
    assert parsed.path == "/avatars/user/photo.png"
    assert 0 < int(parse_qs(parsed.query)["x-oss-expires"][0]) <= 300


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "avatars/user/photo.png?download=1",
        "avatars/user/photo%2Epng",
        "avatars/user/photo\\name.png",
        "avatars/user/nested/photo.png",
        "avatars/user/photo.txt",
        "uploads/avatars/user/photo.png",
    ],
)
async def test_profile_avatar_ignores_malformed_or_unconfirmed_keys(key: str) -> None:
    # 功能: 验证异常或未确认头像路径不会中断用户资料展示
    # 参数: key 为待展示的异常或未确认头像对象键
    provider = AliyunOssProvider(
        "cn-shenzhen",
        "juya-test",
        credentials_provider=cast(Any, oss.credentials.StaticCredentialsProvider("id", "secret")),
    )
    assert await profile_avatar_url("user", key, provider.sign_get_url) is None
