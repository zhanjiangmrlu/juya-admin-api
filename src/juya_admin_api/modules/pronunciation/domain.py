import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Target:
    public_id: str
    confirmed_text: str
    reading_variant: str = ""
    language: str = "en"
    allow_synthesis: bool = True
    uploaded_target_id: str | None = None
    uploaded_resource_version: str | None = None
    status: str = "CONFIRMED"
    config_version: str = "1"
    target_type: str = "WORD"


@dataclass(frozen=True)
class Config:
    voice: int = 101050
    region: str = "ap-guangzhou"
    provider: str = "tencent"
    codec: str = "mp3"
    sample_rate: int = 16000
    speed: float = 0
    volume: float = 0
    model: int = 1
    version: str = "1"
    price_version: str = "unconfigured"
    currency: str = "CNY"
    microcost_per_character: int = 30
    enabled: bool = False


def cache_key(target: Target, config: Config) -> str:
    synthesis = asdict(config)
    for operational_field in ("enabled", "price_version", "currency", "microcost_per_character"):
        synthesis.pop(operational_field)
    value = {
        "text": target.confirmed_text,
        "reading_variant": target.reading_variant,
        "language": target.language,
        "target_config": target.config_version,
        "synthesis": synthesis,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class SynthesisFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        unknown: bool = False,
        request_id: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.unknown = unknown
        self.request_id = request_id


@dataclass(frozen=True)
class AudioResult:
    audio: bytes
    request_id: str
    billed_characters: int
