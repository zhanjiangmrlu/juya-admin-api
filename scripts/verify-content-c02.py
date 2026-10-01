"""C02 authoring tests in a uniquely owned database; no cloud calls or shared cleanup."""

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
PREFIX = "juya_c02_content_20261001_"


def main() -> int:
    database = PREFIX + uuid4().hex[:12]
    inspected = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "juya-admin-api-mysql-1"], text=True, encoding="utf-8"
        )
    )
    settings = dict(item.split("=", 1) for item in inspected[0]["Config"]["Env"] if "=" in item)
    connection = pymysql.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password=settings["MYSQL_ROOT_PASSWORD"],
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
        url = URL.create(
            "mysql+pymysql",
            username="root",
            password=settings["MYSQL_ROOT_PASSWORD"],
            host="127.0.0.1",
            port=3306,
            database=database,
        ).render_as_string(hide_password=False)
        environment = os.environ.copy()
        environment["JUYA_TEST_DATABASE_URL"] = url
        environment.pop("JUYA_TEST_REDIS_URL", None)
        # Ignore any shell migration URL so Alembic cannot use a shared database.
        previous = os.environ.pop("JUYA_MIGRATION_DATABASE_URL", None)
        try:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            command.upgrade(config, "heads")
        finally:
            if previous is not None:
                os.environ["JUYA_MIGRATION_DATABASE_URL"] = previous
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/unit/content",
                "tests/e2e/test_admin_content_editing_flow.py",
                "tests/integration/test_v13_content_transaction.py",
                "-q",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
        )
        print(
            json.dumps(
                {
                    "scope": "C01-C06",
                    "isolated_database": database,
                    "pytest_exit": result.returncode,
                    "cloud_ocr_calls": 0,
                    "oss_uploads": 0,
                }
            )
        )
        return result.returncode
    finally:
        if not database.startswith(PREFIX) or not database[len(PREFIX) :].isalnum():
            raise RuntimeError("Unexpected owned database identifier")
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{database}`")
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
