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
    "securitytoken",
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
    # 功能: 对 URL 中的凭据及敏感查询字段脱敏.
    # 参数:
    #     value: 待扫描 URL 片段并清除敏感查询参数的日志文本.
    # 返回: 敏感 URL 查询参数值已替换的文本;非 URL 内容原样保留.
    def redact(match: re.Match[str]) -> str:
        # 功能: 替换 URL 正则命中的敏感凭据片段.
        # 参数:
        #     match: URL 脱敏正则命中的片段.
        # 返回: 原 URL 的结构,其中敏感查询参数替换为 [REDACTED] 并重新编码.
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
    # 功能: 递归清洗日志内容中的敏感字段和地址凭据.
    # 参数:
    #     value: 待脱敏的日志字段内容,可为字符串,列表或映射.
    #     key: 当前日志字段名,用于识别密码,令牌等敏感字段.
    # 返回: 保留原容器结构的脱敏内容;敏感字段替换为 [REDACTED],非敏感标量原样返回.
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
        # 功能: 清洗日志记录的消息和参数并允许记录继续输出.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        #     record: 待脱敏的 Python 日志记录.
        # 返回: 条件成立或操作成功时为 True,否则为 False.
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
    # 功能: 为第三方 SDK 日志安装脱敏过滤器.
    # 参数: 无.
    # 返回: 无返回值;正常完成表示本次操作成功.
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger) and logger.name.startswith(
            ("alibabacloud", "aliyun")
        ):
            logger.addFilter(SensitiveDataFilter())
            for handler in logger.handlers:
                handler.addFilter(SensitiveDataFilter())


def configure_logging(level: str) -> None:
    # 功能: 配置应用日志级别和敏感信息过滤.
    # 参数:
    #     level: 应用日志级别名称,例如 INFO 或 DEBUG.
    # 返回: 无返回值;正常完成表示本次操作成功.
    handler = logging.StreamHandler()
    handler.addFilter(SensitiveDataFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    protect_sdk_logging()
