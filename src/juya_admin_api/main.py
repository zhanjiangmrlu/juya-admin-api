from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from juya_admin_api.api.health import (
    ReadinessProbe,
    create_health_router,
    default_readiness_probe,
)
from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.observability.logging import configure_logging
from juya_admin_api.infrastructure.observability.request_id import RequestIdMiddleware
from juya_admin_api.infrastructure.runtime import Runtime, build_runtime
from juya_admin_api.shared.errors import install_error_handlers


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    configure_logging(runtime_settings.log_level)
    if runtime_settings.environment not in {"local", "test"}:
        runtime_settings.validate_oss_configuration()
        if runtime_settings.database_url is None:
            raise RuntimeError("JUYA_DATABASE_URL is required outside local/test")
    active_runtime = runtime
    if active_runtime is None and runtime_settings.database_url is not None:
        active_runtime = build_runtime(runtime_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if active_runtime is not None:
            await active_runtime.close()

    app = FastAPI(title="Juya Admin API", version="0.1.0", lifespan=lifespan)
    app.state.settings = runtime_settings
    app.state.runtime = active_runtime
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(
        create_health_router(
            runtime_settings.service_name,
            readiness_probe
            or (active_runtime.readiness_probe if active_runtime else default_readiness_probe),
        )
    )
    if active_runtime is not None:
        for router in active_runtime.routers:
            app.include_router(router)
    return app


app = create_app()
