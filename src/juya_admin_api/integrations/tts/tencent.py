"""Tencent TextToVoice API 2019-08-23; credentials never appear in exceptions."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from juya_admin_api.modules.pronunciation.domain import AudioResult, Config, SynthesisFailure


@dataclass(frozen=True)
class TencentCredentials:
    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    token: str | None = field(default=None, repr=False)


class TencentTtsProvider:
    def __init__(
        self, credentials: Callable[[], TencentCredentials], client: httpx.AsyncClient | None = None
    ):
        self.credentials = credentials
        self.client = client

    async def generate(self, text: str, config: Config, session_id: str) -> AudioResult:
        if not text or len(text.encode("utf-8")) > 500:
            raise SynthesisFailure("TEXT_LIMIT")
        if config.codec not in ("wav", "mp3") or config.sample_rate not in (8000, 16000, 24000):
            raise SynthesisFailure("INVALID_CONFIG")
        body = json.dumps(
            {
                "Text": text,
                "SessionId": session_id,
                "VoiceType": config.voice,
                "Codec": config.codec,
                "SampleRate": config.sample_rate,
                "Speed": config.speed,
                "Volume": config.volume,
                "ModelType": config.model,
                "PrimaryLanguage": 2,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        credentials = self.credentials()
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
        host = "tts.tencentcloudapi.com"
        canonical = (
            "POST\n/\n\ncontent-type:application/json\nhost:"
            + host
            + "\n\ncontent-type;host\n"
            + hashlib.sha256(body).hexdigest()
        )
        scope = date + "/tts/tc3_request"
        string_to_sign = (
            "TC3-HMAC-SHA256\n"
            + str(timestamp)
            + "\n"
            + scope
            + "\n"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )

        def sign(key: bytes, value: str) -> bytes:
            return hmac.new(key, value.encode(), hashlib.sha256).digest()

        key = sign(
            sign(sign(("TC3" + credentials.secret_key).encode(), date), "tts"), "tc3_request"
        )
        signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Host": host,
            "X-TC-Action": "TextToVoice",
            "X-TC-Version": "2019-08-23",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": config.region,
            "Authorization": "TC3-HMAC-SHA256 Credential="
            + credentials.secret_id
            + "/"
            + scope
            + ", SignedHeaders=content-type;host, Signature="
            + signature,
        }
        if credentials.token:
            headers["X-TC-Token"] = credentials.token
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=60, follow_redirects=False)
        try:
            response = await client.post("https://" + host + "/", content=body, headers=headers)
            if response.status_code != 200:
                raise SynthesisFailure("PROVIDER_HTTP_ERROR", unknown=True)
            payload = response.json()["Response"]
            request_id = str(payload.get("RequestId", ""))
            if "Error" in payload:
                code = str(payload["Error"]["Code"])
                retryable = code.startswith(("RequestLimitExceeded", "InternalError"))
                raise SynthesisFailure(code, retryable=retryable, request_id=request_id)
            audio = base64.b64decode(payload["Audio"], validate=True)
            if not audio or len(audio) > 10 * 1024 * 1024:
                raise SynthesisFailure("INVALID_PROVIDER_AUDIO", request_id=request_id)
            # Tencent bills each English letter/punctuation/space as one character.
            return AudioResult(audio, request_id, len(text))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SynthesisFailure("PROVIDER_RESULT_UNKNOWN", unknown=True) from exc
        except (KeyError, ValueError, binascii.Error) as exc:
            raise SynthesisFailure("INVALID_PROVIDER_RESPONSE", unknown=True) from exc
        finally:
            if owned:
                await client.aclose()
