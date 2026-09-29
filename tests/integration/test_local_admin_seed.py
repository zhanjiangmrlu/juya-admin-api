import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from sqlalchemy import create_engine, text

from juya_admin_api.local_admin import (
    LocalAdminConfig,
    SQLAlchemyLocalAdminRepository,
    seed_local_admin,
)

PROJECT_ROOT = Path(__file__).parents[2]
TEST_DATABASE_URL = os.getenv("JUYA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JUYA_TEST_DATABASE_URL is required for MySQL integration tests",
)


def test_local_admin_seed_is_idempotent_and_restores_login_state() -> None:
    """验证真实 MySQL 初始化不会重复账号并会恢复登录状态

    Returns:
        None
    """
    assert TEST_DATABASE_URL is not None
    alembic = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    command.upgrade(alembic, "head")

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM admin_user WHERE username = :username"),
            {"username": "local-seed-admin"},
        )

    config = LocalAdminConfig(
        environment="test",
        database_url=TEST_DATABASE_URL,
        username="local-seed-admin",
        password="first-password",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    repository = SQLAlchemyLocalAdminRepository(TEST_DATABASE_URL)
    try:
        first = seed_local_admin(config, repository, datetime(2026, 9, 29, tzinfo=UTC))
        second = seed_local_admin(
            LocalAdminConfig(
                environment="test",
                database_url=TEST_DATABASE_URL,
                username="local-seed-admin",
                password="updated-password",
                totp_secret="KRUGS4ZANFZSAYJA",
            ),
            repository,
            datetime(2026, 9, 29, 0, 1, tzinfo=UTC),
        )
    finally:
        repository.close()

    with engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT password_hash, totp_secret_ciphertext, status, failed_login_count, "
                    "locked_until, last_totp_step FROM admin_user WHERE username = :username"
                ),
                {"username": "local-seed-admin"},
            )
            .mappings()
            .all()
        )
        connection.execute(
            text("DELETE FROM admin_user WHERE username = :username"),
            {"username": "local-seed-admin"},
        )
    engine.dispose()

    assert first.created is True
    assert second.created is False
    assert len(rows) == 1
    PasswordHasher().verify(rows[0]["password_hash"], "updated-password")
    assert rows[0]["totp_secret_ciphertext"] == b"KRUGS4ZANFZSAYJA"
    assert rows[0]["status"] == "ACTIVE"
    assert rows[0]["failed_login_count"] == 0
    assert rows[0]["locked_until"] is None
    assert rows[0]["last_totp_step"] is None
