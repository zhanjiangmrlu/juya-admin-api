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
