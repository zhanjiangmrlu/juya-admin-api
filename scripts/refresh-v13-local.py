"""Refresh the existing local V1.3 stack without saving or printing runtime secrets."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT / "juya-admin-api"


def inspect(name: str) -> dict:
    return json.loads(
        subprocess.check_output(["docker", "inspect", name], stderr=subprocess.DEVNULL)
    )[0]


def environment(info: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)


def run(command: list[str], *, payload: str | None = None, cwd: Path = REPO) -> None:
    result = subprocess.run(
        command, cwd=cwd, input=payload, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    print(json.dumps({"stage": command[-1], "exit_code": result.returncode}), flush=True)
    if result.returncode:
        # Docker/Alembic errors can contain connection strings; retain them in memory only.
        raise SystemExit("Local refresh failed; runtime secrets were not printed")


def main() -> None:
    original = inspect("juya-admin-api-admin-api-1")
    api = environment(original)
    if api.get("JUYA_ENVIRONMENT") != "local":
        raise SystemExit("Only the existing local stack may be refreshed")
    names = ("admin-api", "admin-worker-content", "admin-worker-domain", "admin-beat")
    api = {key: value for key, value in api.items() if key.startswith(("JUYA_", "OSS_"))}
    api["JUYA_MINIAPP_API_BASE_URL"] = "http://juya-main-mini-api:8000"
    api["JUYA_REQUIRED_SCHEMA_VERSION"] = "16"
    services: dict = {}
    for name in names:
        current = environment(inspect(f"juya-admin-api-{name}-1"))
        current = {
            key: value for key, value in current.items() if key.startswith(("JUYA_", "OSS_"))
        }
        current.update(api)
        current["JUYA_PROCESS_ROLE"] = name
        services[name] = {"environment": current, "image": "juya-v13-unified-admin-local"}
    services["migrate"] = {
        "image": "juya-v13-unified-admin-local",
        "environment": api
        | {
            "JUYA_PROCESS_ROLE": "migrate",
            "JUYA_MIGRATION_DATABASE_URL": api["JUYA_DATABASE_URL"],
        },
    }
    try:
        mini = environment(inspect("juya-main-mini-api"))
    except subprocess.CalledProcessError:
        mini = environment(inspect("juya-v13-mini-api"))
    mini = {key: value for key, value in mini.items() if key.startswith(("JUYA_", "OSS_"))}
    mini.update({key: value for key, value in api.items() if key.startswith(("OSS_", "JUYA_OSS_"))})
    mini.update(
        {
            "JUYA_ENVIRONMENT": "local",
            "JUYA_DATABASE_URL": api["JUYA_DATABASE_URL"].replace(
                "mysql+pymysql://", "mysql+asyncmy://"
            ),
            "JUYA_REDIS_URL": api["JUYA_REDIS_URL"],
            "JUYA_INTERNAL_HMAC_SECRET": api["JUYA_INTERNAL_HMAC_SECRET"],
            "JUYA_ADMIN_API_BASE_URL": "http://juya-admin-api-admin-api-1:8000",
            "JUYA_LOCAL_DEV_MODE": "false",
            "JUYA_CONTENT_SECURITY_ENABLED": api.get("JUYA_CONTENT_SECURITY_ENABLED", "false"),
        }
    )
    mini.pop("JUYA_MIGRATION_DATABASE_URL", None)
    mini.pop("JUYA_PROCESS_ROLE", None)
    services["mini-api"] = {
        "build": "../juya-miniapp-api",
        "image": "juya-v13-mini-local",
        "container_name": "juya-main-mini-api",
        "ports": ["8001:8000"],
        "environment": mini | {"JUYA_PROCESS_TYPE": "api"},
        "read_only": True,
        "tmpfs": ["/tmp"],
    }
    services["mini-worker"] = {
        "image": "juya-v13-mini-local",
        "container_name": "juya-main-mini-worker",
        "environment": mini | {"JUYA_PROCESS_TYPE": "worker", "JUYA_ENABLE_BEAT": "true"},
        "read_only": True,
        "tmpfs": ["/tmp"],
    }
    payload = json.dumps({"services": services})
    project = original["Config"]["Labels"]["com.docker.compose.project"]
    compose = ["docker", "compose", "-p", project, "-f", "docker-compose.dev.yml", "-f", "-"]
    run([*compose, "build", "admin-api", "mini-api"], payload=payload)
    run([*compose, "run", "--rm", "--no-deps", "migrate"], payload=payload)
    run(
        [*compose, "up", "-d", "--no-deps", "--no-build", *names, "mini-api", "mini-worker"],
        payload=payload,
    )
    print("Local stack refreshed: admin 8000, user API 8001; existing environment preserved")


if __name__ == "__main__":
    main()
