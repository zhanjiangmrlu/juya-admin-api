from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LENGTH = 128


def _request_id_from_header(value: str | None) -> str:
    # 功能: 校验客户端请求标识并在缺失或非法时生成新的标识.
    # 参数:
    #     value: 客户端 X-Request-ID 头部内容;缺失或非法时生成新标识.
    # 返回: 通过格式校验的客户端请求标识或新生成的随机标识.
    if value and 0 < len(value) <= _MAX_REQUEST_ID_LENGTH and value.isascii():
        return value
    return uuid4().hex


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 功能: 补充请求关联标识并将请求交给后续处理.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
        #     call_next: 将请求交给后续中间件或路由的异步回调.
        # 返回: 后续路由返回并补充请求标识的 HTTP 响应.
        request_id = _request_id_from_header(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def get_request_id(request: Request) -> str:
    # 功能: 读取请求上下文中的关联标识.
    # 参数:
    #     request: 当前 HTTP 请求,提供头部,路径,请求体及请求关联上下文.
    # 返回: 请求上下文中的关联标识.
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else uuid4().hex
