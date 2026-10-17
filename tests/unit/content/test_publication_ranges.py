import pytest

from juya_admin_api.modules.content.publication_ranges import digest, normalize, verify_facts
from juya_admin_api.shared.errors import AppError


def candidate(revision="r"):
    return dict(
        content_type="short_reading",
        content_id=revision,
        revision_id=revision,
        expected_version=1,
        expected_edit_version=1,
        candidate_hash="a" * 64,
        warning_codes=[],
    )


def test_range_freezes_whole_identity_and_distinct_edit_version():
    values = normalize([candidate("b"), candidate("a")])
    assert [item["revision_id"] for item in values] == ["a", "b"]
    assert digest(values) == digest(normalize(list(reversed(values))))
    changed = [{**values[0], "expected_edit_version": 2}, values[1]]
    assert digest(changed) != digest(values)
    with pytest.raises(AppError):
        normalize([candidate(), candidate()])
    with pytest.raises(AppError):
        normalize([{**candidate(), "expected_edit_version": None}])


def test_actual_readiness_and_snapshot_changes_are_never_accepted():
    frozen = normalize([candidate()])[0]
    verify_facts(frozen, {**frozen, "ready": True})
    for changed in (
        {"ready": False},
        {"expected_version": 2},
        {"candidate_hash": "b" * 64},
        {"warning_codes": ["NEW_WARNING"]},
    ):
        with pytest.raises(AppError):
            verify_facts(frozen, {**frozen, "ready": True, **changed})


def test_freeze_one_thousand_and_reject_over_limit():
    assert len(normalize([candidate(str(index)) for index in range(1000)])) == 1000
    with pytest.raises(AppError):
        normalize([candidate(str(index)) for index in range(1001)])
