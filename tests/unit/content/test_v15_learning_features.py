from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from juya_admin_api.infrastructure.security.service_hmac import ServicePrincipal
from juya_admin_api.modules.content.v15_contracts import SceneDetail
from juya_admin_api.modules.learning_features.router import (
    FeaturesCommand,
    FeaturesRead,
    create_learning_features_router,
)
from juya_admin_api.modules.learning_features.store import LearningFeaturesStore, configuration
from juya_admin_api.modules.short_reading.router import SortOrderRequest
from juya_admin_api.shared.errors import AppError


def test_recording_configuration_fails_closed_and_cannot_be_set_by_internal_read():
    for value in ({}, None, "broken", {"recording_enabled": "true"}, {"recording_enabled": 1}):
        assert configuration(value, 0) == {
            "recording_enabled": False,
            "profile_completion_enabled": False,
            "version": 0,
        }
    assert configuration('{"recording_enabled":true}', 2)["recording_enabled"]
    assert not SceneDetail(
        content_id="c",
        content_type="dialogue",
        content_version=1,
        revision_id="r",
        title_en="a",
        title_zh="甲",
    ).recording_enabled
    with pytest.raises(ValidationError):
        FeaturesRead(recording_enabled=True)
    with pytest.raises(ValidationError):
        FeaturesCommand(recording_enabled="true", expected_version=0)


def test_sort_order_is_bounded_operational_integer():
    assert SortOrderRequest(expected_version=1, sort_order=-2).sort_order == -2
    for value in (True, "1", 2147483648):
        with pytest.raises(ValidationError):
            SortOrderRequest(expected_version=1, sort_order=value)


def test_internal_features_are_restricted_to_bff_and_never_accept_client_switch():
    class Store:
        reads = 0

        async def read(self) -> dict[str, Any]:
            self.reads += 1
            return {"recording_enabled": False, "version": 0}

    store = Store()
    service_name = "untrusted-service"

    async def principal() -> ServicePrincipal:
        return ServicePrincipal(service_name)

    async def admin() -> Any:
        raise AssertionError("internal route never requests admin identity")

    app = FastAPI()
    app.include_router(
        create_learning_features_router(
            cast(LearningFeaturesStore, store),
            current_admin=admin,
            current_admin_write=admin,
            current_service=principal,
        )
    )
    with TestClient(app) as client:
        with pytest.raises(AppError) as denied:
            client.post("/internal/v1/learning-features", json={})
        assert denied.value.code == "SERVICE_PURPOSE_DENIED"
        assert store.reads == 0
        service_name = "juya-miniapp-api"
        assert (
            client.post(
                "/internal/v1/learning-features", json={"recording_enabled": True}
            ).status_code
            == 422
        )
        response = client.post("/internal/v1/learning-features", json={})
        assert response.json() == {
            "recording_enabled": False,
            "profile_completion_enabled": False,
            "version": 0,
        }
        assert response.headers["Cache-Control"] == "no-store"


def test_features_are_strict_independent_and_recording_only_payload_omits_profile_patch():
    assert configuration({"recording_enabled": True, "profile_completion_enabled": "true"}, 4) == {
        "recording_enabled": True,
        "profile_completion_enabled": False,
        "version": 4,
    }
    assert configuration({"recording_enabled": 1, "profile_completion_enabled": True}, 4) == {
        "recording_enabled": False,
        "profile_completion_enabled": True,
        "version": 4,
    }
    assert FeaturesCommand(recording_enabled=False, expected_version=2).model_dump(
        exclude_unset=True
    ) == {"recording_enabled": False, "expected_version": 2}
    for invalid in (1, "true", None):
        with pytest.raises(ValidationError):
            FeaturesCommand(
                recording_enabled=True, profile_completion_enabled=invalid, expected_version=0
            )
    with pytest.raises(ValidationError):
        FeaturesRead(profile_completion_enabled=True)


def test_admin_recording_only_router_preserves_omitted_profile_in_command_digest():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.command.return_value = {
        "recording_enabled": False,
        "profile_completion_enabled": True,
        "version": 8,
    }

    async def admin():
        return SimpleNamespace(admin_user_id="admin")

    app = FastAPI()
    app.include_router(
        create_learning_features_router(
            store, current_admin=admin, current_admin_write=admin, current_service=admin
        )
    )
    response = TestClient(app).put(
        "/api/v1/admin/learning-features",
        json={"recording_enabled": False, "expected_version": 7},
        headers={"Idempotency-Key": "old-body"},
    )
    assert response.status_code == 200 and response.json()["profile_completion_enabled"] is True
    store.command.assert_awaited_once_with(
        "admin", "old-body", {"recording_enabled": False, "expected_version": 7}
    )
