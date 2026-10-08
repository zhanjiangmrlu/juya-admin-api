from collections.abc import Awaitable, Callable, Mapping

from fastapi import APIRouter

from juya_admin_api.shared.errors import AppError

ReadinessProbe = Callable[[], Awaitable[Mapping[str, bool]]]


async def default_readiness_probe() -> Mapping[str, bool]:
    # 功能: 提供默认就绪检查结果.
    # 参数: 无.
    # 返回: 各依赖名称到就绪状态的映射.
    return {"configuration": True}


def create_health_router(service_name: str, readiness_probe: ReadinessProbe) -> APIRouter:
    # 功能: 创建存活及依赖就绪检查路由.
    # 参数:
    #     service_name: 内部调用方或当前服务的名称.
    #     readiness_probe: 检查数据库,缓存等依赖是否就绪的异步回调.
    # 返回: 已注册业务端点的 FastAPI 路由对象.
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live")
    async def live() -> dict[str, str]:
        # 功能: 返回服务存活状态和名称.
        # 参数: 无.
        # 返回: status=ok 和当前 service 名称.
        return {"status": "ok", "service": service_name}

    @router.get("/ready")
    async def ready() -> dict[str, object]:
        # 功能: 检查依赖就绪状态并返回相应 HTTP 结果.
        # 参数: 无.
        # 返回: status=ready 及各依赖检查结果 checks;依赖未就绪时抛出 HTTP 503.
        checks = dict(await readiness_probe())
        if not checks or not all(checks.values()):
            raise AppError(
                code="SERVICE_NOT_READY",
                message="服务尚未就绪",
                status_code=503,
                details={"checks": checks},
            )
        return {"status": "ready", "checks": checks}

    return router
