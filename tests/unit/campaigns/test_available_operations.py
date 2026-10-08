from juya_admin_api.modules.campaigns.repository import campaign_available_operations


def test_campaign_operations_follow_repository_state_rules() -> None:
    # 功能:验证活动可执行操作遵循仓库状态规则。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert campaign_available_operations("DRAFT", True) == ["open", "copy", "capacity"]
    assert campaign_available_operations("OPEN", True) == ["pause", "end", "capacity"]
    assert campaign_available_operations("PAUSED", True) == ["resume", "end", "capacity"]
    assert campaign_available_operations("ENDED", True) == ["archive", "copy", "capacity"]
    assert campaign_available_operations("ARCHIVED", True) == ["capacity"]
    assert campaign_available_operations("DRAFT", False) == []
