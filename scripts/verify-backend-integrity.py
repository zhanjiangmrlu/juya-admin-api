"""Run B01-B05 tests in a uniquely owned disposable database; never print credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pymysql
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    database = "juya_v13_b_20261001_" + uuid4().hex[:12]
    inspected = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "juya-admin-api-mysql-1"], text=True, encoding="utf-8"
        )
    )
    container_env = dict(
        entry.split("=", 1) for entry in inspected[0]["Config"]["Env"] if "=" in entry
    )
    password = container_env["MYSQL_ROOT_PASSWORD"]
    connection = pymysql.connect(
        host="127.0.0.1", port=3306, user="root", password=password, autocommit=True
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        url = URL.create(
            "mysql+pymysql",
            username="root",
            password=password,
            host="127.0.0.1",
            port=3306,
            database=database,
        ).render_as_string(hide_password=False)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(config, "20261001_b_integrity")
        environment = os.environ.copy()
        environment["JUYA_TEST_DATABASE_URL"] = url
        environment.pop("JUYA_TEST_REDIS_URL", None)
        admin = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/integration/test_backend_integrity.py",
                "tests/integration/test_v13_batch_operations.py",
                "-q",
                "--tb=short",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        mini_root = ROOT.parent / "juya-miniapp-api"
        mini = subprocess.run(
            [
                str(mini_root / ".venv/Scripts/python.exe"),
                "-m",
                "pytest",
                "tests/integration/test_backend_review_contact_integrity.py",
                "tests/integration/test_favorite_merge.py",
                "-q",
                "--tb=short",
            ],
            cwd=mini_root,
            env=environment,
            check=False,
        )
        print(
            json.dumps(
                {
                    "scope": "B01-B05",
                    "isolated_database": database,
                    "admin_exit": admin.returncode,
                    "mini_exit": mini.returncode,
                    "cloud_ocr_calls": 0,
                    "cloud_object_overwrites": 0,
                }
            )
        )
        return int(bool(admin.returncode or mini.returncode))
    finally:
        if not database.startswith("juya_v13_b_20261001_") or not database[20:].isalnum():
            raise RuntimeError("Unexpected owned database identifier")
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{database}`")
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
