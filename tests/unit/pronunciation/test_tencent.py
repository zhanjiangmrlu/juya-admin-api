import json

import httpx
import pytest

from juya_admin_api.integrations.tts.tencent import TencentCredentials, TencentTtsProvider
from juya_admin_api.modules.pronunciation.domain import Config, SynthesisFailure


@pytest.mark.asyncio
async def test_signed_real_api_payload_and_base64_result():
    def respond(request):
        assert request.url.host == "tts.tencentcloudapi.com"
        assert request.headers["X-TC-Action"] == "TextToVoice"
        assert request.headers["Authorization"].startswith("TC3-HMAC-SHA256 ")
        body = json.loads(request.content)
        assert body["SessionId"] == "unique-attempt"
        assert body["Text"] == "hello"
        assert body["PrimaryLanguage"] == 2
        return httpx.Response(200, json={"Response": {"Audio": "YXVkaW8=", "RequestId": "rid"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = TencentTtsProvider(lambda: TencentCredentials("test-id", "test-key"), client)
        result = await provider.generate("hello", Config(), "unique-attempt")
    assert result.audio == b"audio"
    assert result.request_id == "rid"


@pytest.mark.asyncio
async def test_transport_unknown_and_no_credentials_in_error():
    def respond(request):
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = TencentTtsProvider(lambda: TencentCredentials("test-id", "test-key"), client)
        with pytest.raises(SynthesisFailure) as caught:
            await provider.generate("hello", Config(), "unique-attempt")
    assert caught.value.unknown
    assert "test-key" not in str(caught.value)
