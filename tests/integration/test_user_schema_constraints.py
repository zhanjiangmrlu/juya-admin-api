import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

TEST_DATABASE_URL = os.getenv("JUYA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="JUYA_TEST_DATABASE_URL must point to an isolated MySQL 8.4 database",
)


@pytest.fixture(scope="module")
def connection() -> Connection:
    # 功能:迁移测试库并提供连接,用例结束后释放数据库资源。
    # 参数:无。
    # 返回:测试资源生成器;产生数据库连接或会话后,在退出时释放资源。
    assert TEST_DATABASE_URL is not None
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    with engine.connect() as active_connection:
        yield active_connection
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_user_data(connection: Connection) -> None:
    # 功能:按依赖顺序清理测试用户领域表并提交清理结果。
    # 参数:
    #     connection: 测试数据库连接;隔离脚本中为 MySQL 密码与端口的二元组。
    # 返回:无;完成模拟状态更新、调用记录或检查。
    ordered_tables = [
        "miniapp_outbox",
        "user_command_dedup",
        "account_deletion_request",
        "inbox_message",
        "review_session",
        "favorite_source",
        "favorite_entry",
        "daily_checkin",
        "learning_completion_event",
        "learning_progress",
        "contact_correction_request",
        "contact_status_history",
        "user_contact",
        "user_session",
        "user_profile",
        "user_app_identity",
        "user_account",
    ]
    connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    for table_name in ordered_tables:
        connection.execute(text(f"DELETE FROM `{table_name}`"))
    connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    connection.commit()


def create_user(connection: Connection) -> int:
    # 功能:在独立测试库创建参数化用例需要的用户记录。
    # 参数:
    #     connection: 测试数据库连接;隔离脚本中为 MySQL 密码与端口的二元组。
    # 返回:整数。
    result = connection.execute(
        text(
            "INSERT INTO user_account "
            "(public_id, juya_number, status, created_at) "
            "VALUES ('01J00000000000000000000000', 'JUYA-1', 'ACTIVE', UTC_TIMESTAMP(6))"
        )
    )
    connection.commit()
    assert result.lastrowid is not None
    return int(result.lastrowid)


@pytest.mark.parametrize(
    ("first_insert", "duplicate_insert"),
    [
        (
            "INSERT INTO user_app_identity "
            "(user_id, app_id, openid_ciphertext, openid_hmac, created_at) "
            "VALUES (:user_id, 'wx-app', X'01', UNHEX(REPEAT('11', 32)), UTC_TIMESTAMP(6))",
            "INSERT INTO user_app_identity "
            "(user_id, app_id, openid_ciphertext, openid_hmac, created_at) "
            "VALUES (:user_id, 'wx-app', X'02', UNHEX(REPEAT('11', 32)), UTC_TIMESTAMP(6))",
        ),
        (
            "INSERT INTO learning_progress "
            "(user_id, scene_id, source_type, position, last_client_sequence, started_at, "
            "last_learned_at) VALUES (:user_id, 'scene-1', 'SCENE', JSON_OBJECT(), 1, "
            "UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
            "INSERT INTO learning_progress "
            "(user_id, scene_id, source_type, position, last_client_sequence, started_at, "
            "last_learned_at) VALUES (:user_id, 'scene-1', 'SCENE', JSON_OBJECT(), 2, "
            "UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
        ),
        (
            "INSERT INTO learning_completion_event "
            "(user_id, scene_id, completed_at, idempotency_key) "
            "VALUES (:user_id, 'scene-1', UTC_TIMESTAMP(6), 'complete-1')",
            "INSERT INTO learning_completion_event "
            "(user_id, scene_id, completed_at, idempotency_key) "
            "VALUES (:user_id, 'scene-2', UTC_TIMESTAMP(6), 'complete-1')",
        ),
        (
            "INSERT INTO daily_checkin (user_id, beijing_date, completion_source) "
            "VALUES (:user_id, '2026-09-28', 'SCENE')",
            "INSERT INTO daily_checkin (user_id, beijing_date, completion_source) "
            "VALUES (:user_id, '2026-09-28', 'REVIEW')",
        ),
        (
            "INSERT INTO favorite_entry "
            "(public_id, user_id, entry_type, normalized_key, entry_stable_id, favorited_at) "
            "VALUES ('01J00000000000000000000010', :user_id, 'VOCABULARY', 'hello', "
            "'entry-1', UTC_TIMESTAMP(6))",
            "INSERT INTO favorite_entry "
            "(public_id, user_id, entry_type, normalized_key, entry_stable_id, favorited_at) "
            "VALUES ('01J00000000000000000000011', :user_id, 'VOCABULARY', 'hello', "
            "'entry-2', UTC_TIMESTAMP(6))",
        ),
        (
            "INSERT INTO account_deletion_request "
            "(public_id, user_id, requested_at, effective_at, status) "
            "VALUES ('01J00000000000000000000020', :user_id, UTC_TIMESTAMP(6), "
            "DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 7 DAY), 'PENDING')",
            "INSERT INTO account_deletion_request "
            "(public_id, user_id, requested_at, effective_at, status) "
            "VALUES ('01J00000000000000000000021', :user_id, UTC_TIMESTAMP(6), "
            "DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 7 DAY), 'PENDING')",
        ),
        (
            "INSERT INTO contact_correction_request "
            "(public_id, user_id, reason, status, created_at) "
            "VALUES ('01J00000000000000000000030', :user_id, 'incorrect', 'PENDING', "
            "UTC_TIMESTAMP(6))",
            "INSERT INTO contact_correction_request "
            "(public_id, user_id, reason, status, created_at) "
            "VALUES ('01J00000000000000000000031', :user_id, 'still incorrect', "
            "'PROCESSING', UTC_TIMESTAMP(6))",
        ),
    ],
)
def test_user_domain_unique_constraints(
    connection: Connection, first_insert: str, duplicate_insert: str
) -> None:
    # 功能:验证用户领域数据库唯一约束。
    # 参数:
    #     connection: 测试数据库连接;隔离脚本中为 MySQL 密码与端口的二元组。
    #     first_insert: 首次插入的数据库记录,供唯一约束检查。
    #     duplicate_insert: 与首次记录冲突的插入数据,预期触发唯一约束。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    user_id = create_user(connection)
    connection.execute(text(first_insert), {"user_id": user_id})
    connection.commit()

    with pytest.raises(IntegrityError):
        connection.execute(text(duplicate_insert), {"user_id": user_id})
        connection.commit()
    connection.rollback()
