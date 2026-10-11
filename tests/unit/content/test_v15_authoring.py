import pytest

from juya_admin_api.modules.content.v15_authoring import validate_boundary
from juya_admin_api.shared.errors import AppError


def test_boundary_requires_actual_source_duration_and_explicit_manual_check():
    assert validate_boundary(0, 100, 100, True).end_ms == 100
    for args in ((0, 101, 100, True), (0, 100, 100, False), (-1, 1, 100, True)):
        with pytest.raises((AppError, ValueError)):
            validate_boundary(*args)
