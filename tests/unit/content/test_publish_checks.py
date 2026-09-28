import pytest

from juya_admin_api.modules.content.publish_checks import (
    PublishFacts,
    evaluate_publish_checks,
    require_publishable,
)
from juya_admin_api.shared.errors import AppError


def test_errors_block_and_warnings_require_explicit_acknowledgement() -> None:
    error_checks = evaluate_publish_checks(
        PublishFacts(has_title=False, has_entries=True, media_ready=True, has_audio=True)
    )
    with pytest.raises(AppError) as blocked:
        require_publishable(error_checks, frozenset())
    assert blocked.value.code == "PUBLISH_CHECK_FAILED"

    warning_checks = evaluate_publish_checks(
        PublishFacts(has_title=True, has_entries=True, media_ready=True, has_audio=False)
    )
    with pytest.raises(AppError) as warning:
        require_publishable(warning_checks, frozenset())
    assert warning.value.code == "PUBLISH_WARNING_NOT_ACKNOWLEDGED"

    summary = require_publishable(warning_checks, frozenset({"AUDIO_MISSING"}))
    assert summary.ready is True
    assert summary.warning_codes == ("AUDIO_MISSING",)
