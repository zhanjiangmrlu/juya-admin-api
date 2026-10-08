from urllib.parse import parse_qs

import httpx
import pytest
from test_v13_media import BytesOss, png_bytes

from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_baidu_general_reads_bytes_and_retains_locations_and_confidence() -> None:
    # 功能:验证百度通用 OCR 读取字节并保留位置及置信度。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.integrations.ocr.baidu import BaiduOcrProvider

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        # 功能:检查请求路径、参数或认证头并返回预设 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
        requests.append(request)
        if request.url.path == "/oauth/2.0/token":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        return httpx.Response(
            200,
            json={
                "log_id": 42,
                "words_result": [
                    {
                        "words": "Hello",
                        "location": {"left": 3, "top": 4, "width": 20, "height": 8},
                        "probability": {"average": 0.97},
                    }
                ],
                "paragraphs_result": [{"words_result_idx": [0]}],
            },
        )

    provider = BaiduOcrProvider(
        BytesOss(png_bytes()),
        "fixture-key",
        "fixture-secret",
        transport=httpx.MockTransport(handle),
    )
    result = await provider.recognize("uploads/images/admin-1/a.png", "dialogue")
    assert result.provider_request_id == "42"
    assert result.text == "Hello"
    assert result.blocks[0]["location"]["left"] == 3
    assert result.blocks[0]["confidence"] == 0.97
    assert result.blocks[0]["paragraph"] == {"index": 0, "line_indices": [0]}
    params = parse_qs(requests[1].content.decode())
    assert requests[1].url.path == "/rest/2.0/ocr/v1/general"
    assert params["language_type"] == ["CHN_ENG"]
    assert params["paragraph"] == params["probability"] == ["true"]


@pytest.mark.asyncio
async def test_baidu_rejects_small_image_before_any_network_call() -> None:
    # 功能:验证百度 OCR 在网络请求前拒绝过小图片。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.integrations.ocr.baidu import BaiduOcrProvider

    def handle(request: httpx.Request) -> httpx.Response:
        # 功能:检查请求路径、参数或认证头并返回预设 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:无;完成模拟状态更新、调用记录或检查。
        pytest.fail("invalid image must not invoke external provider")

    provider = BaiduOcrProvider(
        BytesOss(png_bytes(14, 15)), "key", "secret", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(AppError) as error:
        await provider.recognize("uploads/images/admin-1/a.png", "dialogue")
    assert error.value.code == "OCR_IMAGE_INVALID"


@pytest.mark.asyncio
async def test_baidu_failure_retains_response_log_id_without_another_call() -> None:
    # 功能:验证百度 OCR 失败保留日志标识且不重复调用。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    from juya_admin_api.integrations.ocr.baidu import BaiduOcrProvider

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        # 功能:检查请求路径、参数或认证头并返回预设 HTTP 响应。
        # 参数:
        #     request: 传入的 HTTP 或 SDK 请求,供测试检查请求头、请求体及目标资源。
        # 返回:预设 HTTP 响应。
        calls.append(request.url.path)
        if request.url.path == "/oauth/2.0/token":
            return httpx.Response(200, json={"access_token": "fixture"})
        return httpx.Response(
            200, json={"log_id": 123456789, "error_code": 17, "error_msg": "fixture"}
        )

    provider = BaiduOcrProvider(
        BytesOss(png_bytes()), "id", "secret", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(AppError) as error:
        await provider.recognize("uploads/images/admin-1/a.png", "dialogue")
    assert error.value.details["provider_request_id"] == "123456789"
    assert len(calls) == 2
