"""Run database tests against a named isolated DB; never print credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

root = Path(__file__).resolve().parents[2]
repo = root / sys.argv[1]
if repo not in [root / "juya-admin-api", root / "juya-miniapp-api"]:
    raise SystemExit("Unknown test repository")
info = json.loads(subprocess.check_output(["docker", "inspect", "juya-admin-api-mysql-1"]))[0]
variables = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
password = variables["MYSQL_ROOT_PASSWORD"]
database = os.getenv("JUYA_V13_TEST_DATABASE", "juya_v13_test")
if not database.startswith("juya_v13_") or not database.replace("_", "").isalnum():
    raise SystemExit("Isolated test database name required")
subprocess.run(
    [
        "docker",
        "exec",
        "juya-admin-api-mysql-1",
        "sh",
        "-c",
        f'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS {database} '
        'CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"',
    ],
    check=True,
)
env = dict(os.environ)
env.pop("JUYA_MIGRATION_DATABASE_URL", None)
env["PYTHONIOENCODING"] = "utf-8"
env["JUYA_TEST_DATABASE_URL"] = (
    f"mysql+pymysql://root:{quote(password, safe='')}@127.0.0.1:3306/{database}"
)
env["JUYA_TEST_REDIS_URL"] = "redis://127.0.0.1:6398/14"
result = subprocess.run(
    [str(repo / ".venv/Scripts/python.exe"), "-m", "pytest", *sys.argv[2:]],
    cwd=repo,
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
output = result.stdout.decode("utf-8", errors="replace").replace(password, "[redacted]")
print(output)
raise SystemExit(result.returncode)
