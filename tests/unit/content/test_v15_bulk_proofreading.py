import pytest

from juya_admin_api.modules.content.v15_proofreading import normal_candidate, normalize_candidates
from juya_admin_api.shared.errors import AppError


def test_normal_classification_is_server_fact_not_client_flag():
    assert normal_candidate({"ready": True}, [])
    assert not normal_candidate({"ready": True}, [{"code": "PAIRING_AMBIGUOUS"}])
    assert not normal_candidate({"ready": False}, [])
    assert not normal_candidate({"ready": True, "normal": False}, [])


def test_client_cannot_split_failure_group():
    item = {
        "revision_id": "r",
        "candidate_hash": "h",
        "content_type": "word_card",
        "content_id": "c",
    }
    assert normalize_candidates([item])[0]["group_id"] == "word_card"
    with pytest.raises(AppError, match="校对分组"):
        normalize_candidates([item | {"group_id": "isolated"}])
