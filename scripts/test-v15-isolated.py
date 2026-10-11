"""Provision a private MySQL when the shared stack is stopped; never restart it."""

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, **kwargs)  # type: ignore[arg-type]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    name = "juya-v15-private-mysql-" + uuid4().hex
    password = secrets.token_urlsafe(32)
    created = False
    try:
        result = run(
            [
                "docker",
                "create",
                "--name",
                name,
                "-p",
                "127.0.0.1::3306",
                "-e",
                "MYSQL_ROOT_PASSWORD=" + password,
                "mysql:8.4",
            ]
        )
        if result.returncode:
            print("Private MySQL creation failed")
            return 1
        created = True
        if run(["docker", "start", name]).returncode:
            print("Private MySQL start failed")
            return 1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ping = run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot -e "SELECT 1"',
                ]
            )
            if not ping.returncode:
                break
            time.sleep(1)
        else:
            print("Private MySQL readiness timed out")
            return 1
        print("Private MySQL ready; isolated runner owns UUID databases and Redis")
        env = dict(os.environ, JUYA_ISOLATED_MYSQL_CONTAINER=name, PYTHONIOENCODING="utf-8")
        process = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/test-v13-isolated.py"),
                "juya-admin-api",
                *sys.argv[1:],
            ],
            cwd=root,
            env=env,
        )
        return process.returncode
    finally:
        if created:
            run(["docker", "rm", "-f", name])


if __name__ == "__main__":
    raise SystemExit(main())
