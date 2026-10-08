from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from argon2 import PasswordHasher

import juya_admin_api.local_admin as local_admin_module
from juya_admin_api.local_admin import LocalAdminConfig, main, seed_local_admin

BASE_ENVIRONMENT: Mapping[str, str] = {
    "JUYA_ENVIRONMENT": "local",
    "JUYA_MIGRATION_DATABASE_URL": "mysql+pymysql://root:password@127.0.0.1:3306/juya",
}


class RecordingLocalAdminRepository:
    """记录本地管理员初始化参数的测试 repository"""

    def __init__(self, *, created: bool) -> None:
        # 功能:初始化 RecordingLocalAdminRepository 测试替身的预设数据和调用记录。
        # 参数:
        #     self: 当前 RecordingLocalAdminRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     created: 仓库 upsert 预设的是否新建状态。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        """创建记录型 repository

        Args:
            created: upsert 应返回的创建状态

        Returns:
            None
        """
        self.created = created
        self.closed = False
        self.values: dict[str, object] = {}

    def upsert_admin(
        self,
        *,
        public_id: str,
        username: str,
        password_hash: str,
        totp_secret: bytes,
    ) -> bool:
        # 功能:记录管理员初始化字段并返回预设是否新建状态。
        # 参数:
        #     self: 当前 RecordingLocalAdminRepository 测试替身实例,保存本用例的预设状态或调用记录。
        #     public_id: 管理员公开标识,用于初始化管理员记录。
        #     username: 测试管理员登录名,用于查询认证账户。
        #     password_hash: 初始化管理员时传给仓库的 Argon2 密码哈希。
        #     totp_secret: 传给管理员仓库的 TOTP 字节字段,测试检查占位值。
        # 返回:布尔值。
        """记录本地管理员 upsert 参数

        Args:
            public_id: 管理员公开编号
            username: 管理员登录名
            password_hash: Argon2 密码哈希
            totp_secret: 兼容现有数据库字段的固定占位值

        Returns:
            预设的创建状态
        """
        self.values = {
            "password_hash": password_hash,
            "public_id": public_id,
            "totp_secret": totp_secret,
            "username": username,
        }
        return self.created

    def close(self) -> None:
        # 功能:模拟资源关闭;记录或更新关闭状态供清理断言。
        # 参数:
        #     self: 当前 RecordingLocalAdminRepository 测试替身实例,保存本用例的预设状态或调用记录。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        """记录 repository 已释放

        Returns:
            None
        """
        self.closed = True


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_admin_config_accepts_local_and_test(environment: str) -> None:
    # 功能:验证本地管理员配置允许 local 和 test 环境。
    # 参数:
    #     environment: 参数化测试选择的运行环境,例如 local、test 或 production。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    """验证本地管理员配置允许隔离环境

    Args:
        environment: 需要验证的运行环境

    Returns:
        None
    """
    config = LocalAdminConfig.from_environment(
        {**BASE_ENVIRONMENT, "JUYA_ENVIRONMENT": environment}
    )

    assert config.environment == environment
    assert config.username == "admin"
    assert config.password == "JuyaLocal@2026"
    assert not hasattr(config, "totp_secret")


def test_local_admin_config_rejects_production() -> None:
    # 功能:验证本地管理员配置拒绝生产环境。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    """验证生产环境在数据库连接前拒绝本地初始化

    Returns:
        None
    """
    with pytest.raises(ValueError, match="JUYA_ENVIRONMENT must be local or test"):
        LocalAdminConfig.from_environment({**BASE_ENVIRONMENT, "JUYA_ENVIRONMENT": "production"})


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("JUYA_MIGRATION_DATABASE_URL", "JUYA_MIGRATION_DATABASE_URL is required"),
        ("JUYA_LOCAL_ADMIN_USERNAME", "JUYA_LOCAL_ADMIN_USERNAME is required"),
        ("JUYA_LOCAL_ADMIN_PASSWORD", "JUYA_LOCAL_ADMIN_PASSWORD is required"),
    ],
)
def test_local_admin_config_rejects_blank_values(key: str, message: str) -> None:
    # 功能:验证本地管理员配置拒绝空白字段。
    # 参数:
    #     key: 需要置为空值的本地管理员环境变量名称。
    #     message: 参数化测试使用的日志消息,用于检查敏感字段脱敏。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    """验证关键配置为空时初始化失败

    Args:
        key: 需要置空的环境变量名
        message: 期望的错误提示

    Returns:
        None
    """
    with pytest.raises(ValueError, match=message):
        LocalAdminConfig.from_environment({**BASE_ENVIRONMENT, key: ""})


@pytest.mark.parametrize("created", [True, False])
def test_seed_local_admin_hashes_credentials_and_reports_upsert(created: bool) -> None:
    # 功能:验证初始化管理员哈希密码并报告 upsert 结果。
    # 参数:
    #     created: 仓库 upsert 预设的是否新建状态。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    """验证初始化使用正式哈希并返回创建或更新结果

    Args:
        created: repository 返回的创建状态

    Returns:
        None
    """
    config = LocalAdminConfig.from_environment(BASE_ENVIRONMENT)
    repository = RecordingLocalAdminRepository(created=created)

    result = seed_local_admin(
        config,
        repository,
        datetime(2026, 9, 29, tzinfo=UTC),
    )

    assert result.created is created
    assert result.username == "admin"
    assert repository.values["username"] == "admin"
    assert len(str(repository.values["public_id"])) == 26
    assert repository.values["totp_secret"] == b"PASSWORD_ONLY_LOGIN"
    PasswordHasher().verify(str(repository.values["password_hash"]), "JuyaLocal@2026")


def test_main_seeds_admin_without_printing_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 功能:验证主入口初始化管理员且不打印凭证。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    #     capsys: pytest 标准输出与错误捕获工具,用于检查诊断及脱敏内容。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    """验证命令入口初始化管理员且不输出认证密钥

    Args:
        monkeypatch: pytest 环境和依赖替换工具
        capsys: pytest 标准输出捕获工具

    Returns:
        None
    """
    repository = RecordingLocalAdminRepository(created=True)
    monkeypatch.setenv("JUYA_ENVIRONMENT", "local")
    monkeypatch.setenv("JUYA_MIGRATION_DATABASE_URL", "mysql+pymysql://local")
    monkeypatch.setenv("JUYA_LOCAL_ADMIN_PASSWORD", "hidden-password")
    # 匿名函数: 注入记录调用的管理员仓库, 隔离实际数据库连接。
    # 参数:
    #     _database_url: 初始化入口传入的数据库 URL, 替身忽略连接而返回内存仓库。
    # 返回: 当前用例创建的内存仓库实例。
    monkeypatch.setattr(
        local_admin_module,
        "SQLAlchemyLocalAdminRepository",
        lambda _database_url: repository,
    )

    assert main() == 0

    output = capsys.readouterr().out
    assert "admin" in output
    assert "hidden-password" not in output
    assert repository.closed is True
