import httpx
import pytest

from juya_admin_api.integrations.asr.aliyun import AliyunASR
from juya_admin_api.integrations.content_ai.aliyun import AliyunContentAI


@pytest.mark.asyncio
async def test_asr_no_timestamps_never_fabricates_alignment():
    def reply(request):
        return httpx.Response(200, json={"transcripts": [{"text": "Hello there."}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await AliyunASR(client, "secret", result_hosts=("result.example",)).parse_result(
            "https://result.example/a"
        )
    assert result.boundaries == ()
    assert result.requires_manual_alignment


@pytest.mark.asyncio
async def test_candidate_rejects_unknown_source_reference():
    def reply(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"candidates":[{"source_id":"invented",'
                            '"english":"hello","chinese":"你好"}]}'
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="source"):
            await AliyunContentAI(client, "secret").suggest({"s1": "hello"})


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized():
    def reply(request):
        return httpx.Response(403, text="secret credential")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="provider request failed") as error:
            await AliyunContentAI(client, "secret").suggest({"s1": "hello"})
    assert "secret" not in str(error.value)
