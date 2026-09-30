import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "authorization",
    "token",
    "openid",
    "openid_ciphertext",
    "wechat_id",
    "wechat_id_ciphertext",
    "description",
    "feedback_body",
    "body",
    "accesskeyid",
    "accesskeysecret",
    "access_key_id",
    "access_key_secret",
    "oss_access_key_id",
    "oss_access_key_secret",
    "security_token",
    "session_token",
    "x-oss-security-token",
    "x-oss-signature",
    "x-oss-credential",
    "signature",
    "policy",
    "ossaccesskeyid",
}
_SIGNED_QUERY_KEYS = _SENSITIVE_KEYS | {"security-token"}


def _redact_url(value: str) -> str:
    def redact(match: re.Match[str]) -> str:
        parts = urlsplit(match.group())
        query = [
            (key, "[REDACTED]" if key.casefold() in _SIGNED_QUERY_KEYS else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    return re.sub(r'https?://[^\s\'"]+', redact, value)


def redact_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_url(value)
    return value


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("alibabacloud", "aliyun", "oss.")):
            # SDK exceptions can embed complete request bodies and credentials.
            record.msg = "OSS SDK event (details suppressed)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        else:
            record.msg = redact_value(record.msg)
            if record.args:
                record.args = redact_value(record.args)
        for key, value in tuple(record.__dict__.items()):
            if key not in {"msg", "args", "exc_info"}:
                record.__dict__[key] = redact_value(value, key=key)
        return True


def protect_sdk_logging() -> None:
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger) and logger.name.startswith(
            ("alibabacloud", "aliyun")
        ):
            logger.addFilter(SensitiveDataFilter())
            for handler in logger.handlers:
                handler.addFilter(SensitiveDataFilter())


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SensitiveDataFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    protect_sdk_logging()
