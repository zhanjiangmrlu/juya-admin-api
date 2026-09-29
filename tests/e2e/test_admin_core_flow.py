from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter
from fastapi.testclient import TestClient

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.runtime import Runtime
from juya_admin_api.main import create_app


class FakeRuntime:
    def __init__(self) -> None:
        router = APIRouter(prefix="/api/v1/admin")

        @router.get("/runtime-smoke")
        async def runtime_smoke() -> dict[str, bool]:
            return {"assembled": True}

        self.routers = (router,)
        self.readiness_probe = self.ready
        self.closed = False

    async def ready(self) -> Mapping[str, bool]:
        return {"mysql": True, "redis": True, "schema": True, "configuration": True}

    async def close(self) -> None:
        self.closed = True


def test_runtime_routes_health_and_lifecycle_are_assembled() -> None:
    runtime = FakeRuntime()
    app = create_app(Settings(), runtime=cast(Runtime, cast(Any, runtime)))

    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/admin/runtime-smoke").json() == {"assembled": True}

    assert runtime.closed is True


def test_container_runs_as_non_root_and_keeps_migration_separate() -> None:
    root = Path(__file__).parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (root / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    aliyun_pipeline = (root / ".aliyun-ci.yml").read_text(encoding="utf-8")
    ecs_compose = (root / "deploy" / "docker-compose.ecs.yml").read_text(encoding="utf-8")

    assert "USER 10001:10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "migrate)" in entrypoint
    assert "alembic upgrade head" in entrypoint
    assert "admin-api)" in entrypoint
    assert "--schedule /tmp/celerybeat-schedule" in entrypoint
    assert "component: VMDeploy" in aliyun_pipeline
    assert "step: ACRDockerBuild" in aliyun_pipeline
    assert "aliyun-kubectl" not in aliyun_pipeline
    assert "read_only: true" in ecs_compose
    assert "JUYA_PROCESS_ROLE: admin-beat" in ecs_compose


def test_local_compose_seeds_admin_after_migration_before_api() -> None:
    """验证本地管理员初始化角色与 Compose 依赖顺序

    Returns:
        None
    """
    root = Path(__file__).parents[2]
    shell_entrypoint = (root / "scripts" / "entrypoint.sh").read_text(encoding="utf-8")
    powershell_entrypoint = (root / "scripts" / "entrypoint.ps1").read_text(encoding="utf-8")
    local_compose = (root / "docker-compose.dev.yml").read_text(encoding="utf-8")
    ecs_compose = (root / "deploy" / "docker-compose.ecs.yml").read_text(encoding="utf-8")

    assert "seed-local-admin)" in shell_entrypoint
    assert "python -m juya_admin_api.local_admin" in shell_entrypoint
    assert "'seed-local-admin'" in powershell_entrypoint
    assert "python '-m' 'juya_admin_api.local_admin'" in powershell_entrypoint

    assert "  seed-local-admin:" in local_compose
    seed_block = local_compose.split("  seed-local-admin:", maxsplit=1)[1].split(
        "\n  admin-api:", maxsplit=1
    )[0]
    admin_api_block = local_compose.split("  admin-api:", maxsplit=1)[1].split(
        "\n  admin-worker-content:", maxsplit=1
    )[0]
    assert "JUYA_PROCESS_ROLE: seed-local-admin" in seed_block
    assert "migrate:\n        condition: service_completed_successfully" in seed_block
    assert "JUYA_LOCAL_ADMIN_USERNAME: ${JUYA_LOCAL_ADMIN_USERNAME:-admin}" in seed_block
    assert "JUYA_LOCAL_ADMIN_PASSWORD: ${JUYA_LOCAL_ADMIN_PASSWORD:-JuyaLocal@2026}" in seed_block
    assert (
        "JUYA_LOCAL_ADMIN_TOTP_SECRET: ${JUYA_LOCAL_ADMIN_TOTP_SECRET:-JBSWY3DPEHPK3PXP}"
        in seed_block
    )
    assert "seed-local-admin:\n        condition: service_completed_successfully" in admin_api_block
    assert "seed-local-admin" not in ecs_compose
