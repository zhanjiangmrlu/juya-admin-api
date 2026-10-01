from urllib.parse import parse_qs

import httpx
import pytest
from test_v13_media import BytesOss, png_bytes

from juya_admin_api.shared.errors import AppError


@pytest.mark.asyncio
async def test_baidu_general_reads_bytes_and_retains_locations_and_confidence() -> None:
    from juya_admin_api.integrations.ocr.baidu import BaiduOcrProvider

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
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
    from juya_admin_api.integrations.ocr.baidu import BaiduOcrProvider

    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid image must not invoke external provider")

    provider = BaiduOcrProvider(
        BytesOss(png_bytes(14, 15)), "key", "secret", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(AppError) as error:
        await provider.recognize("uploads/images/admin-1/a.png", "dialogue")
    assert error.value.code == "OCR_IMAGE_INVALID"
