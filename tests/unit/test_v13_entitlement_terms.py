from juya_admin_api.modules.formal_entitlements.domain import EntitlementTerm


def test_terms_are_lowercase_public_contract_values() -> None:
    # 功能:验证期限类型使用公开契约约定的小写值。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    assert {term.value for term in EntitlementTerm} == {
        "month_1",
        "month_2",
        "month_3",
        "month_6",
        "month_12",
        "permanent",
    }
