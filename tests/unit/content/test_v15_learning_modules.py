import pytest
from pydantic import ValidationError

from juya_admin_api.modules.learning_features.modules import LearningModulesStore
from juya_admin_api.modules.learning_features.modules_router import ModuleCommand


def test_readiness_uses_actual_capabilities_and_cannot_be_overridden_by_client():
    store = LearningModulesStore(None, {"scene_learning": True, "grammar": False})
    assert store.project({"module_type": "scene_learning", "enabled": 1})["ready"]
    result = store.project({"module_type": "grammar", "enabled": 0})
    assert result["ready"] is False and result["reason"] == "MODULE_NOT_IMPLEMENTED"
    assert store.project({"module_type": "short_reading", "enabled": 0})["ready"] is False
    with pytest.raises(ValidationError):
        ModuleCommand(
            expected_version=1, display_name="Grammar", sort_order=0, enabled=True, ready=True
        )


def test_module_metadata_command_has_strict_bounded_fields():
    values = dict(expected_version=1, display_name="短文", sort_order=1, enabled=False)
    for field, invalid in (
        ("sort_order", True),
        ("sort_order", -1),
        ("sort_order", 65536),
        ("sort_order", 2147483648),
        ("expected_version", 0),
        ("enabled", "true"),
        ("display_name", ""),
    ):
        with pytest.raises(ValidationError):
            ModuleCommand(**(values | {field: invalid}))
