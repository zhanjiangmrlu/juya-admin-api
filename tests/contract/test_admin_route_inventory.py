import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app

CONTACT_PATHS = {
    "/api/v1/admin/contact-corrections": {"get"},
    "/api/v1/admin/contact-corrections/{correction_id}": {"get"},
    "/api/v1/admin/contact-corrections/{correction_id}/commands/{command}": {"post"},
    "/api/v1/admin/users/{user_id}/commands/contact-status": {"post"},
    "/api/v1/admin/users/{user_id}/commands/verify-contact-change": {"post"},
    "/api/v1/admin/users/{user_id}/contact-copy-events": {"post"},
    "/api/v1/admin/users": {"get"},
    "/api/v1/admin/users/search-by-wechat": {"post"},
    "/api/v1/admin/users/{user_id}": {"get"},
}

ENTITLEMENT_CAMPAIGN_PATHS = {
    "/api/v1/admin/entitlements": {"get"},
    "/api/v1/admin/formal-entitlements/{entitlement_id}": {"get"},
    "/api/v1/admin/limited-entitlements/{entitlement_id}": {"get"},
    "/api/v1/admin/content-packages": {"get"},
    "/api/v1/admin/campaigns": {"get", "post"},
    "/api/v1/admin/campaigns/{campaign_id}": {"get", "put"},
    "/api/v1/admin/campaigns/{campaign_id}/versions/copy": {"post"},
    "/api/v1/admin/campaigns/{campaign_id}/commands/{operation}": {"post"},
}

FEEDBACK_PATHS = {
    "/api/v1/admin/feedback": {"get"},
    "/api/v1/admin/feedback/{ticket_id}": {"get"},
    "/api/v1/admin/feedback/{ticket_id}/screenshot-url": {"post"},
    "/api/v1/admin/feedback/{ticket_id}/internal-notes": {"post"},
    "/api/v1/admin/feedback/{ticket_id}/commands/start": {"post"},
    "/api/v1/admin/feedback/{ticket_id}/commands/request-supplement": {"post"},
    "/api/v1/admin/feedback/{ticket_id}/commands/resolve": {"post"},
    "/api/v1/admin/feedback/{ticket_id}/commands/close-insufficient": {"post"},
}

CONTENT_EDITING_PATHS = {
    "/api/v1/admin/content/scenes": {"get"},
    "/api/v1/admin/content/scenes/{scene_id}": {"get"},
    "/api/v1/admin/content/revisions/{revision_id}": {"get", "put"},
    "/api/v1/admin/content/discovery-config": {"get", "put"},
    "/api/v1/admin/content/revisions/{revision_id}/preview": {"get"},
}

MEDIA_JOB_PATHS = {
    "/api/v1/admin/media/ocr/jobs": {"post"},
    "/api/v1/admin/media/ocr/jobs/{job_id}": {"get"},
    "/api/v1/admin/media/ocr/jobs/{job_id}/candidate": {"get"},
    "/api/v1/admin/media/ocr/jobs/{job_id}/commands/{operation}": {"post"},
    "/api/v1/admin/media/audio-targets": {"get"},
    "/api/v1/admin/media/audio-targets/{target_id}/versions": {"get", "post"},
    "/api/v1/admin/media/audio-targets/{target_id}/commands/generate": {"post"},
    "/api/v1/admin/media/audio-versions/{version_id}/commands/confirm": {"post"},
    "/api/v1/admin/media/audio-targets/{target_id}/commands/rollback": {"post"},
    "/api/v1/admin/media/batch-jobs": {"get", "post"},
    "/api/v1/admin/media/batch-jobs/{batch_id}": {"get"},
    "/api/v1/admin/media/batch-jobs/{batch_id}/commands/{operation}": {"post"},
    "/api/v1/admin/media/trash": {"get", "post"},
    "/api/v1/admin/media/trash/{entry_id}/commands/{operation}": {"post"},
}


def _openapi(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "test-access-secret")
    settings = Settings(
        database_url=SecretStr("mysql+pymysql://root:test@127.0.0.1/juya_admin_test"),
        redis_url=SecretStr("redis://127.0.0.1:6379/15"),
        internal_hmac_secret=SecretStr("test-internal-secret"),
        oss_region="oss-cn-test",
        oss_bucket="juya-test",
    )
    return create_app(settings).openapi()


def _parameter_names(operation: Mapping[str, Any]) -> set[str]:
    return {str(parameter["name"]).lower() for parameter in operation.get("parameters", [])}


def _response_schema(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    responses = operation["responses"]
    for status in ("200", "201", "204"):
        content = responses.get(status, {}).get("content", {})
        if "application/json" in content:
            return content["application/json"]["schema"]
    return {}


def _walk_property_names(
    node: object,
    components: Mapping[str, Any],
    *,
    visited: set[str] | None = None,
) -> Iterator[str]:
    visited = visited or set()
    if isinstance(node, list):
        for item in node:
            yield from _walk_property_names(item, components, visited=visited)
        return
    if not isinstance(node, Mapping):
        return
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", maxsplit=1)[-1]
        if name in visited:
            return
        visited.add(name)
        yield from _walk_property_names(components[name], components, visited=visited)
        return
    properties = node.get("properties", {})
    if isinstance(properties, Mapping):
        for name, value in properties.items():
            yield str(name)
            yield from _walk_property_names(value, components, visited=visited)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        if key in node:
            yield from _walk_property_names(node[key], components, visited=visited)


def test_batch_one_admin_routes_and_security_headers_are_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    paths = schema["paths"]

    for path, methods in CONTACT_PATHS.items():
        assert path in paths
        assert methods <= set(paths[path])

    write_paths = (
        "/api/v1/admin/contact-corrections/{correction_id}/commands/{command}",
        "/api/v1/admin/users/{user_id}/commands/contact-status",
        "/api/v1/admin/users/{user_id}/commands/verify-contact-change",
        "/api/v1/admin/users/{user_id}/contact-copy-events",
    )
    for path in write_paths:
        assert "x-csrf-token" in _parameter_names(paths[path]["post"])
    decision = paths["/api/v1/admin/contact-corrections/{correction_id}/commands/{command}"]["post"]
    assert "x-idempotency-key" in _parameter_names(decision)


def test_batch_one_sensitive_response_models_are_explicit_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    components = schema["components"]["schemas"]
    names: set[str] = set()
    for path, methods in CONTACT_PATHS.items():
        for method in methods:
            names.update(
                _walk_property_names(_response_schema(schema["paths"][path][method]), components)
            )

    assert {
        "wechat_id",
        "contact_status",
        "change_pending",
        "verified_at",
        "open_scene_completed_count",
        "learning_days",
        "favorite_count",
    } <= names
    serialized_names = json.dumps(sorted(names)).lower()
    for forbidden in ("openid", "ciphertext", "encrypted", "permanent_media_url"):
        assert forbidden not in serialized_names


def test_batch_two_routes_publish_pagination_and_write_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    paths = schema["paths"]
    for path, methods in ENTITLEMENT_CAMPAIGN_PATHS.items():
        assert path in paths
        assert methods <= set(paths[path])
    for path in (
        "/api/v1/admin/entitlements",
        "/api/v1/admin/content-packages",
        "/api/v1/admin/campaigns",
    ):
        properties = set(
            _walk_property_names(
                _response_schema(paths[path]["get"]), schema["components"]["schemas"]
            )
        )
        assert {"items", "page", "page_size", "total"} <= properties
    for path, method in (
        ("/api/v1/admin/campaigns", "post"),
        ("/api/v1/admin/campaigns/{campaign_id}", "put"),
        ("/api/v1/admin/campaigns/{campaign_id}/versions/copy", "post"),
        ("/api/v1/admin/campaigns/{campaign_id}/commands/{operation}", "post"),
    ):
        names = _parameter_names(paths[path][method])
        assert {"x-csrf-token", "x-idempotency-key"} <= names


def test_campaign_responses_require_server_available_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    components = schema["components"]["schemas"]
    for name in ("CampaignResponse", "CampaignListItemResponse"):
        assert "available_operations" in components[name]["properties"]
        assert "available_operations" in components[name]["required"]


def test_batch_three_feedback_routes_publish_aggregate_models_and_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    paths = schema["paths"]
    for path, methods in FEEDBACK_PATHS.items():
        assert path in paths
        assert methods <= set(paths[path])
    page_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/feedback"]["get"]),
            schema["components"]["schemas"],
        )
    )
    assert {"items", "page", "page_size", "total", "sla_state"} <= page_properties
    detail_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/feedback/{ticket_id}"]["get"]),
            schema["components"]["schemas"],
        )
    )
    assert {"timeline", "screenshots", "rounds", "replies", "internal_notes"} <= detail_properties
    assert "url" not in detail_properties
    for path in (
        "/api/v1/admin/feedback/{ticket_id}/screenshot-url",
        "/api/v1/admin/feedback/{ticket_id}/internal-notes",
    ):
        assert "x-csrf-token" in _parameter_names(paths[path]["post"])
    assert "x-idempotency-key" in _parameter_names(
        paths["/api/v1/admin/feedback/{ticket_id}/internal-notes"]["post"]
    )


def test_batch_four_content_routes_publish_versions_and_write_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    paths = schema["paths"]
    components = schema["components"]["schemas"]
    for path, methods in CONTENT_EDITING_PATHS.items():
        assert path in paths
        assert methods <= set(paths[path])

    page_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/content/scenes"]["get"]),
            components,
        )
    )
    assert {"items", "page", "page_size", "total", "draft_revision_id"} <= page_properties

    revision_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/content/revisions/{revision_id}"]["get"]),
            components,
        )
    )
    assert {"version", "content", "stable_sentence_ids", "stable_entry_ids"} <= revision_properties

    config_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/content/discovery-config"]["get"]),
            components,
        )
    )
    assert {
        "version",
        "open_scene_ids",
        "preview_by_series",
        "learning_modules",
    } <= config_properties
    for path in (
        "/api/v1/admin/content/revisions/{revision_id}",
        "/api/v1/admin/content/discovery-config",
    ):
        assert "x-csrf-token" in _parameter_names(paths[path]["put"])


def test_batch_five_media_routes_publish_job_models_and_write_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _openapi(monkeypatch)
    paths = schema["paths"]
    components = schema["components"]["schemas"]
    for path, methods in MEDIA_JOB_PATHS.items():
        assert path in paths
        assert methods <= set(paths[path])

    job_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/media/ocr/jobs/{job_id}"]["get"]),
            components,
        )
    )
    assert {"id", "status", "provider_request_id", "error_code"} <= job_properties

    batch_properties = set(
        _walk_property_names(
            _response_schema(paths["/api/v1/admin/media/batch-jobs/{batch_id}"]["get"]),
            components,
        )
    )
    assert {"items", "total_count", "success_count", "failure_count"} <= batch_properties

    for path, method in (
        ("/api/v1/admin/media/ocr/jobs", "post"),
        ("/api/v1/admin/media/ocr/jobs/{job_id}/commands/{operation}", "post"),
        ("/api/v1/admin/media/audio-targets/{target_id}/versions", "post"),
        ("/api/v1/admin/media/audio-targets/{target_id}/commands/generate", "post"),
        ("/api/v1/admin/media/audio-versions/{version_id}/commands/confirm", "post"),
        ("/api/v1/admin/media/audio-targets/{target_id}/commands/rollback", "post"),
        ("/api/v1/admin/media/batch-jobs", "post"),
        ("/api/v1/admin/media/batch-jobs/{batch_id}/commands/{operation}", "post"),
        ("/api/v1/admin/media/trash", "post"),
        ("/api/v1/admin/media/trash/{entry_id}/commands/{operation}", "post"),
    ):
        names = _parameter_names(paths[path][method])
        assert {"x-csrf-token", "x-idempotency-key"} <= names
