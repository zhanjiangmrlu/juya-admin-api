from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.formal_entitlements.domain import add_natural_months


@pytest.mark.parametrize(
    ("base", "months", "expected"),
    [
        (
            datetime(2023, 1, 31, 2, 30, tzinfo=UTC),
            1,
            datetime(2023, 2, 28, 2, 30, tzinfo=UTC),
        ),
        (
            datetime(2024, 1, 31, 2, 30, tzinfo=UTC),
            1,
            datetime(2024, 2, 29, 2, 30, tzinfo=UTC),
        ),
        (
            datetime(2025, 12, 31, 16, 15, tzinfo=UTC),
            2,
            datetime(2026, 2, 28, 16, 15, tzinfo=UTC),
        ),
    ],
)
def test_add_natural_months_uses_beijing_calendar(
    base: datetime, months: int, expected: datetime
) -> None:
    assert add_natural_months(base, months) == expected  # type: ignore[arg-type]
