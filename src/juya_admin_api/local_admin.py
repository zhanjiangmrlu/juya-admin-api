import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Self

from sqlalchemy import Engine, create_engine, text

from juya_admin_api.modules.admin_auth.service import hash_password
from juya_admin_api.shared.ids import new_ulid

DEFAULT_LOCAL_ADMIN_USERNAME = "admin"
DEFAULT_LOCAL_ADMIN_PASSWORD = "JuyaLocal@2026"
DEFAULT_LOCAL_ADMIN_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


@dataclass(frozen=True, slots=True)
class LocalAdminConfig:
    """本地管理员初始化配置"""

    environment: str
    database_url: str
    username: str
    password: str
    totp_secret: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """从环境变量读取并校验本地管理员配置

        Args:
            environ: 提供配置值的环境变量映射

        Returns:
            校验完成的本地管理员配置

        Raises:
            ValueError: 环境不安全或必填配置为空
        """
        environment = environ.get("JUYA_ENVIRONMENT", "local").strip()
        if environment not in {"local", "test"}:
            raise ValueError("JUYA_ENVIRONMENT must be local or test")

        return cls(
            environment=environment,
            database_url=_required_value(environ, "JUYA_MIGRATION_DATABASE_URL"),
            username=_required_value(
                environ,
                "JUYA_LOCAL_ADMIN_USERNAME",
                DEFAULT_LOCAL_ADMIN_USERNAME,
            ),
            password=_required_value(
                environ,
                "JUYA_LOCAL_ADMIN_PASSWORD",
                DEFAULT_LOCAL_ADMIN_PASSWORD,
            ),
            totp_secret=_required_value(
                environ,
                "JUYA_LOCAL_ADMIN_TOTP_SECRET",
                DEFAULT_LOCAL_ADMIN_TOTP_SECRET,
            ),
        )


@dataclass(frozen=True, slots=True)
class SeedResult:
    """本地管理员初始化结果"""

    created: bool
    username: str


class LocalAdminRepository(Protocol):
    """本地管理员持久化端口"""

    def upsert_admin(
        self,
        *,
        public_id: str,
        username: str,
        password_hash: str,
        totp_secret: bytes,
    ) -> bool:
        """创建或重置本地管理员

        Args:
            public_id: 首次创建时使用的管理员公开编号
            username: 管理员登录名
            password_hash: Argon2 密码哈希
            totp_secret: 本地环境允许保存的 TOTP 明文密钥

        Returns:
            是否创建了新管理员
        """
        ...


class SQLAlchemyLocalAdminRepository:
    """使用同步 SQLAlchemy 连接初始化本地管理员"""

    def __init__(self, database_url: str) -> None:
        """创建 MySQL 本地管理员 repository

        Args:
            database_url: Alembic 使用的同步数据库地址

        Returns:
            None
        """
        self._engine: Engine = create_engine(database_url)

    def upsert_admin(
        self,
        *,
        public_id: str,
        username: str,
        password_hash: str,
        totp_secret: bytes,
    ) -> bool:
        """创建新管理员或重置同名管理员的登录状态

        Args:
            public_id: 首次创建时使用的管理员公开编号
            username: 管理员登录名
            password_hash: Argon2 密码哈希
            totp_secret: 本地环境允许保存的 TOTP 明文密钥

        Returns:
            是否创建了新管理员
        """
        with self._engine.begin() as connection:
            admin_id = connection.scalar(
                text("SELECT id FROM admin_user WHERE username = :username FOR UPDATE"),
                {"username": username},
            )
            values = {
                "password_hash": password_hash,
                "public_id": public_id,
                "totp_secret": totp_secret,
                "username": username,
            }
            if admin_id is None:
                connection.execute(
                    text(
                        "INSERT INTO admin_user "
                        "(public_id, username, password_hash, totp_secret_ciphertext, status) "
                        "VALUES (:public_id, :username, :password_hash, :totp_secret, 'ACTIVE')"
                    ),
                    values,
                )
                return True

            connection.execute(
                text(
                    "UPDATE admin_user SET password_hash = :password_hash, "
                    "totp_secret_ciphertext = :totp_secret, status = 'ACTIVE', "
                    "failed_login_count = 0, locked_until = NULL, last_totp_step = NULL, "
                    "updated_at = UTC_TIMESTAMP(6) WHERE id = :admin_id"
                ),
                {**values, "admin_id": admin_id},
            )
            return False

    def close(self) -> None:
        """释放 repository 持有的数据库连接池

        Returns:
            None
        """
        self._engine.dispose()


def seed_local_admin(
    config: LocalAdminConfig,
    repository: LocalAdminRepository,
    now: datetime,
) -> SeedResult:
    """创建或重置本地管理员

    Args:
        config: 已校验的本地管理员配置
        repository: 管理员持久化端口
        now: 生成公开编号使用的当前时间

    Returns:
        管理员创建或更新结果
    """
    created = repository.upsert_admin(
        public_id=new_ulid(now),
        username=config.username,
        password_hash=hash_password(config.password),
        totp_secret=config.totp_secret.encode("utf-8"),
    )
    return SeedResult(created=created, username=config.username)


def main() -> int:
    """读取本地配置并初始化管理员账号

    Returns:
        成功时返回零退出码
    """
    config = LocalAdminConfig.from_environment()
    repository = SQLAlchemyLocalAdminRepository(config.database_url)
    try:
        result = seed_local_admin(config, repository, datetime.now(UTC))
    finally:
        repository.close()

    action = "created" if result.created else "updated"
    print(f"Local admin {result.username} {action}")
    return 0


def _required_value(
    environ: Mapping[str, str],
    key: str,
    default: str | None = None,
) -> str:
    """读取去除首尾空白后的必填配置

    Args:
        environ: 环境变量映射
        key: 需要读取的环境变量名
        default: 变量缺失时使用的默认值

    Returns:
        非空配置值

    Raises:
        ValueError: 配置值为空
    """
    value = environ.get(key, default or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
