from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app


def test_production_cannot_start_as_health_only_without_oss_configuration() -> None:
    # 功能:验证生产环境缺少 OSS 配置时不能仅以健康接口启动。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    with pytest.raises(RuntimeError, match="OSS"):
        create_app(Settings(environment="production"))


@pytest.mark.parametrize(
    "environment,bucket,expected",
    [
        ("test", "juya", "juya-test"),
        ("production", "juya-test", "juya"),
    ],
)
def test_explicit_bucket_binding_rejects_cross_environment_configuration(
    environment: str,
    bucket: str,
    expected: str,
) -> None:
    # 功能:验证明确桶绑定拒绝跨环境配置。
    # 参数:
    #     environment: 参数化测试选择的运行环境,例如 local、test 或 production。
    #     bucket: OSS 测试桶名称,用于检查明确的环境绑定。
    #     expected: 参数化测试提供的预期结果,用于与实际返回值比较。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    settings = Settings(
        environment=environment,
        oss_region="cn-shenzhen",
        oss_bucket=bucket,
        oss_expected_bucket=expected,
    )
    with pytest.raises(RuntimeError, match="bucket"):
        settings.validate_oss_configuration()


def test_empty_optional_sts_expiration_is_treated_as_unset(monkeypatch) -> None:
    # 功能:验证空的可选 STS 到期时间视为未配置。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    monkeypatch.setenv("JUYA_OSS_CREDENTIALS_EXPIRES_AT", "")
    assert Settings().oss_credentials_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["JUYA_OSS_", "OSS_"])
async def test_runtime_rotates_complete_environment_sts_bundle(monkeypatch, prefix: str) -> None:
    # 功能:验证运行时更新完整环境 STS 凭证组。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     prefix: 参数化测试使用的对象路径前缀。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.infrastructure import runtime as wiring
    from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider

    now = datetime.now(UTC)
    for name in ("ACCESS_KEY_ID", "ACCESS_KEY_SECRET", "SESSION_TOKEN"):
        monkeypatch.delenv("JUYA_OSS_" + name, raising=False)
        monkeypatch.delenv("OSS_" + name, raising=False)
    monkeypatch.setenv(prefix + "ACCESS_KEY_ID", "old-id")
    monkeypatch.setenv(prefix + "ACCESS_KEY_SECRET", "old-secret")
    monkeypatch.setenv(prefix + "SESSION_TOKEN", "old-token")
    monkeypatch.setenv("JUYA_OSS_CREDENTIALS_EXPIRES_AT", (now + timedelta(seconds=90)).isoformat())
    captured: list[AliyunOssProvider] = []

    def capture(*args: Any, **kwargs: Any) -> AliyunOssProvider:
        # 功能:创建并记录使用固定测试时钟的 OSS 适配器。
        # 参数:
        #     args: 被替换调用的位置参数;命令替身中为可执行文件及命令行参数列表。
        #     kwargs: 被替换调用的关键字参数,保留调用方传入的选项供测试检查。
        # 返回:AliyunOssProvider,由本用例预设的数据或所组装的测试资源构成。
        # 匿名函数: 注入固定测试时间或 UTC 当前时间, 控制接口和签名的时间源。
        # 参数: 无。
        # 返回: 对应测试时间或 UTC 当前时间。
        provider = AliyunOssProvider(*args, **kwargs, clock=lambda: now)
        captured.append(provider)
        return provider

    monkeypatch.setattr(wiring, "AliyunOssProvider", capture)
    resources = wiring.build_runtime(
        Settings(
            environment="test",
            database_url="mysql+asyncmy://test:test@localhost/test",
            redis_url="redis://localhost:6399/0",
            internal_hmac_secret="x" * 32,
            oss_region="cn-shenzhen",
            oss_bucket="juya-test",
            oss_expected_bucket="juya-test",
        )
    )
    try:
        first = await captured[0].create_upload_policy("uploads/images/admin-1/", 1024, 300)
        monkeypatch.setenv(prefix + "ACCESS_KEY_ID", "new-id")
        monkeypatch.setenv(prefix + "ACCESS_KEY_SECRET", "new-secret")
        monkeypatch.setenv(prefix + "SESSION_TOKEN", "new-token")
        monkeypatch.setenv(
            "JUYA_OSS_CREDENTIALS_EXPIRES_AT", (now + timedelta(seconds=900)).isoformat()
        )
        second = await captured[0].create_upload_policy("uploads/images/admin-1/", 1024, 300)
        assert first.fields["x-oss-credential"].startswith("old-id/")
        assert first.expires_in == 60
        assert second.fields["x-oss-credential"].startswith("new-id/")
        assert second.fields["x-oss-security-token"] == "new-token"
        assert second.expires_in == 300
    finally:
        await resources.close()
