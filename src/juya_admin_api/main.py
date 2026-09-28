from fastapi import FastAPI

from juya_admin_api.api.health import (
    ReadinessProbe,
    create_health_router,
    default_readiness_probe,
)
from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.observability.request_id import RequestIdMiddleware
from juya_admin_api.shared.errors import install_error_handlers


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()
    app = FastAPI(title="Juya Admin API", version="0.1.0")
    app.state.settings = runtime_settings
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(
        create_health_router(
            runtime_settings.service_name,
            readiness_probe or default_readiness_probe,
        )
    )
    return app


app = create_app()
