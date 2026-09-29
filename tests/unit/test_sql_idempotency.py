import pytest

from juya_admin_api.shared import idempotency


@pytest.mark.parametrize(
    ("stored", "expected"),
    [('{"status":"OPEN"}', {"status": "OPEN"}), ({"status": "OPEN"}, {"status": "OPEN"})],
)
def test_json_idempotency_response_accepts_driver_decoded_json(
    stored: object, expected: dict[str, object]
) -> None:
    assert hasattr(idempotency, "_json_body")
    assert idempotency._json_body(stored) == expected
