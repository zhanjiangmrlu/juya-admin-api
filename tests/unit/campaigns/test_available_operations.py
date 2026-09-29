from juya_admin_api.modules.campaigns.repository import campaign_available_operations


def test_campaign_operations_follow_repository_state_rules() -> None:
    assert campaign_available_operations("DRAFT", True) == ["open", "copy", "capacity"]
    assert campaign_available_operations("OPEN", True) == ["pause", "end", "capacity"]
    assert campaign_available_operations("PAUSED", True) == ["resume", "end", "capacity"]
    assert campaign_available_operations("ENDED", True) == ["archive", "copy", "capacity"]
    assert campaign_available_operations("ARCHIVED", True) == ["capacity"]
    assert campaign_available_operations("DRAFT", False) == []
