from juya_admin_api.modules.formal_entitlements.domain import EntitlementTerm


def test_terms_are_lowercase_public_contract_values() -> None:
    assert {term.value for term in EntitlementTerm} == {
        "month_1",
        "month_2",
        "month_3",
        "month_6",
        "month_12",
        "permanent",
    }
