import pytest

from juya_admin_api.shared import idempotency


@pytest.mark.parametrize(
    ("stored", "expected"),
    [('{"status":"OPEN"}', {"status": "OPEN"}), ({"status": "OPEN"}, {"status": "OPEN"})],
)
def test_json_idempotency_response_accepts_driver_decoded_json(
    stored: object, expected: dict[str, object]
) -> None:
    # 功能:验证 JSON 幂等响应接受驱动已解码的对象。
    # 参数:
    #     stored: 驱动返回的幂等响应,可为 JSON 文本或已解码对象。
    #     expected: 参数化测试提供的预期结果,用于与实际返回值比较。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert hasattr(idempotency, "_json_body")
    assert idempotency._json_body(stored) == expected
