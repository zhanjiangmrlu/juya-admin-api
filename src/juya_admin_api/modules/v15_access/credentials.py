"""Short lived server credentials; keys remain in runtime, never in snapshots."""

import base64
import hashlib
import hmac
import json
from datetime import datetime
from typing import Any

from juya_admin_api.shared.errors import AppError


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def details_hash(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class CredentialCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("credential signing key must have at least 16 bytes")
        self.secret = secret

    def sign(self, claims: dict[str, Any]) -> str:
        payload = base64.urlsafe_b64encode(canonical(claims)).decode().rstrip("=")
        signature = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + signature

    def verify(self, token: str, now: datetime) -> dict[str, Any]:
        try:
            payload, signature = token.split(".")
            expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(claims, dict):
                raise ValueError("claims")
            expiry = datetime.fromisoformat(claims["expires_at"])
            if expiry.tzinfo is None or now >= expiry:
                raise ValueError("expired")
            return dict(claims)
        except (ValueError, KeyError, TypeError) as error:
            raise AppError(
                "CONFIRMATION_CREDENTIAL_INVALID", "收藏确认凭据无效或已过期", 409
            ) from error
