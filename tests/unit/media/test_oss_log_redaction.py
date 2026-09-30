import logging

from juya_admin_api.infrastructure.observability.logging import SensitiveDataFilter


def test_oss_form_fields_and_urls_are_removed_from_log_messages() -> None:
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
