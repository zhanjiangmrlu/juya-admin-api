import logging

from juya_admin_api.infrastructure.observability.logging import SensitiveDataFilter


def test_oss_form_fields_and_urls_are_removed_from_log_messages() -> None:
    # 功能:验证日志删除 OSS 表单字段及签名 URL。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    record = logging.LogRecord(
        "oss",
        logging.ERROR,
        __file__,
        1,
        {
            "AccessKeySecret": "key-secret",
            "x-oss-security-token": "sts-secret",
            "policy": "encoded-secret",
            "url": "https://oss.test/a?x-oss-signature=sig-secret&x-oss-credential=id-secret",
            "request_id": "safe-id",
        },
        (),
        None,
    )
    SensitiveDataFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "safe-id" in record.getMessage()


def test_oss_sdk_tracebacks_are_not_emitted() -> None:
    # 功能:验证 OSS SDK 异常堆栈不会输出。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    error = RuntimeError("private-credential")
    record = logging.LogRecord(
        "alibabacloud_oss_v2.client",
        logging.ERROR,
        __file__,
        1,
        "request failed",
        (),
        (RuntimeError, error, None),
    )
    SensitiveDataFilter().filter(record)
    assert record.exc_info is None
    assert record.exc_text is None


def test_nested_sdk_security_token_spelling_is_redacted() -> None:
    # 功能:验证嵌套 SDK 安全令牌字段也会脱敏。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    record = logging.LogRecord(
        "application",
        logging.INFO,
        __file__,
        1,
        "credentials=%s",
        ({"Credentials": {"SecurityToken": "synthetic-private-token"}},),
        None,
    )
    SensitiveDataFilter().filter(record)
    assert "synthetic-private-token" not in record.getMessage()
