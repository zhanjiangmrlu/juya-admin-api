import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from juya_admin_api.infrastructure.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "versions" / "0009_admin_contact_capabilities.py"


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), "the schema 9 migration must exist"
    spec = importlib.util.spec_from_file_location("admin_contact_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contact_admin_migration_adds_idempotency_and_list_index() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    call_names = [name for name, _args, _kwargs in operations.calls]
    assert call_names == [
        "drop_constraint",
        "execute",
        "execute",
        "create_check_constraint",
        "add_column",
        "add_column",
        "add_column",
        "create_unique_constraint",
        "create_index",
        "execute",
    ]
    execute_sql = [str(args[0]) for name, args, _kwargs in operations.calls if name == "execute"]
    assert execute_sql == [
        "UPDATE user_contact SET contact_status = 'CONTACTED' WHERE contact_status = 'VERIFIED'",
        "UPDATE user_contact SET contact_status = 'UNREACHABLE' WHERE contact_status = 'INVALID'",
        "UPDATE schema_version SET version = 9, updated_at = UTC_TIMESTAMP(6) WHERE id = 1",
    ]
    check_call = operations.calls[3]
    assert check_call[1] == (
        "ck_user_contact_status",
        "user_contact",
        "contact_status IN ('NOT_PROVIDED','PENDING','CONTACTED','UNREACHABLE','DO_NOT_CONTACT')",
    )
    unique_call = operations.calls[7]
    assert unique_call[1] == (
        "uq_contact_correction_actor_idempotency",
        "contact_correction_request",
        ["processed_by", "decision_idempotency_key"],
    )
    index_call = operations.calls[8]
    assert index_call[1] == (
        "ix_contact_correction_status_created",
        "contact_correction_request",
        ["status", "created_at"],
    )
    assert Settings().required_schema_version == 15


@pytest.mark.skipif(
    not os.getenv("JUYA_TEST_DATABASE_URL"),
    reason="JUYA_TEST_DATABASE_URL must point to an isolated MySQL 8.4 database",
)
def test_contact_admin_migration_upgrades_legacy_status_rows() -> None:
    database_url = os.environ["JUYA_TEST_DATABASE_URL"]
    alembic = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.downgrade(alembic, "base")
    command.upgrade(alembic, "0008")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            connection.execute(text("DELETE FROM user_contact"))
            connection.execute(text("DELETE FROM user_account"))
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            connection.execute(
                text(
                    "INSERT INTO user_account "
                    "(id, public_id, juya_number, status, created_at) VALUES "
                    "(900001, '01J00000000000000000900001', 'JUYA-900001', 'ACTIVE', "
                    "UTC_TIMESTAMP(6)), "
                    "(900002, '01J00000000000000000900002', 'JUYA-900002', 'ACTIVE', "
                    "UTC_TIMESTAMP(6))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO user_contact (user_id, contact_status, updated_at) VALUES "
                    "(900001, 'VERIFIED', UTC_TIMESTAMP(6)), "
                    "(900002, 'INVALID', UTC_TIMESTAMP(6))"
                )
            )
        command.upgrade(alembic, "0009")
        with engine.connect() as connection:
            statuses = (
                connection.execute(
                    text(
                        "SELECT contact_status FROM user_contact "
                        "WHERE user_id IN (900001, 900002) ORDER BY user_id"
                    )
                )
                .scalars()
                .all()
            )
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() "
                        "AND table_name = 'contact_correction_request'"
                    )
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    text(
                        "SELECT DISTINCT index_name FROM information_schema.statistics "
                        "WHERE table_schema = DATABASE() "
                        "AND table_name = 'contact_correction_request'"
                    )
                ).scalars()
            )
            check_clause = connection.scalar(
                text(
                    "SELECT check_clause FROM information_schema.check_constraints "
                    "WHERE constraint_schema = DATABASE() "
                    "AND constraint_name = 'ck_user_contact_status'"
                )
            )
            version = connection.scalar(text("SELECT version FROM schema_version WHERE id = 1"))
        assert statuses == ["CONTACTED", "UNREACHABLE"]
        assert {
            "processed_by",
            "decision_idempotency_key",
            "decision_request_hash",
        } <= columns
        assert {
            "uq_contact_correction_actor_idempotency",
            "ix_contact_correction_status_created",
        } <= indexes
        assert check_clause is not None
        assert "UNREACHABLE" in str(check_clause)
        assert "DO_NOT_CONTACT" in str(check_clause)
        assert version == 9
    finally:
        engine.dispose()
        command.upgrade(alembic, "head")
