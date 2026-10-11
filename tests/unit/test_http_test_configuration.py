import pytest

from juya_admin_api.infrastructure.config import Settings


def test_http_cookie_is_secure_by_default() -> None:
    # 验证默认配置保持安全 Cookie
    assert Settings(environment="test").allow_insecure_http is False


def test_http_cookie_can_only_be_enabled_in_test() -> None:
    # 验证仅测试运行环境允许 HTTP Cookie
    assert Settings(environment="test", allow_insecure_http=True).allow_insecure_http


@pytest.mark.parametrize("environment", ["local", "production", "staging"])
def test_other_environments_reject_insecure_http(environment: str) -> None:
    # 参数 environment 指待验证的非测试运行环境
    with pytest.raises(ValueError, match="only allowed in test"):
        Settings(environment=environment, allow_insecure_http=True)
