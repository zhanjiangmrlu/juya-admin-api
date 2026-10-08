from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from juya_admin_api.infrastructure.observability.request_id import get_request_id


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: Mapping[str, object] | None = None,
    ) -> None:
        # 功能: 初始化业务错误对象并保存依赖及运行状态.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     code: 对外返回的稳定业务错误码.
        #     message: 向调用方说明失败原因的错误文本.
        #     status_code: HTTP 响应状态码.
        #     details: 允许向客户端返回的错误补充信息.
        # 返回: 无返回值;正常完成表示本次操作成功.
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


def _error_body(
    request: Request,
    *,
    code: str,
    message: str,
    details: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    # 功能: 构造包含错误码,说明,详情和请求标识的统一错误响应.
    # 参数:
    #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
    #     code: 对外返回的稳定业务错误码.
    #     message: 向调用方说明失败原因的错误文本.
    #     details: 允许向客户端返回的错误补充信息.
    # 返回: 包含错误码,错误说明,详情及请求标识的统一错误结构.
    return {
        "code": code,
        "message": message,
        "request_id": get_request_id(request),
        "details": dict(details or {}),
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    # 功能: 安装业务错误及请求参数校验错误的统一处理器.
    # 参数:
    #     app: 安装路由或异常处理器的 FastAPI 应用.
    # 返回: 无返回值;正常完成表示本次操作成功.
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # 功能: 将业务异常转换为结构化 JSON 响应.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     exc: 框架捕获到的业务或请求校验异常.
        # 返回: 包含统一业务错误结构的 JSON 响应.
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                request,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 功能: 将框架参数校验异常转换为统一业务错误响应.
        # 参数:
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     exc: 框架捕获到的业务或请求校验异常.
        # 返回: 包含统一业务错误结构的 JSON 响应.
        safe_errors = [
            {
                "type": error["type"],
                "loc": list(error["loc"]),
                "msg": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body(
                request,
                code="VALIDATION_ERROR",
                message="请求参数不正确",
                details={"errors": safe_errors},
            ),
        )
