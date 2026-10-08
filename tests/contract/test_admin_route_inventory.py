import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.main import create_app


def test_all_mounted_operations_match_generated_frontend_and_final_documentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证全部已挂载接口与前端 OpenAPI 快照及最终接口文档一致。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    schema = _openapi(monkeypatch)
    workspace = Path(__file__).resolve().parents[3]
    snapshot_path = workspace / "juya-admin/openapi/admin-api.json"
    document_path = workspace / "doc/接口文档/juya-admin-api-接口文档.md"
    if not snapshot_path.exists() or not document_path.exists():
        pytest.skip("full workspace is required for cross-repository documentation acceptance")
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    assert snapshot == schema
    document = document_path.read_text("utf-8")
    documented = set(re.findall(r"\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`", document))
    mounted = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert mounted == documented
    assert len(mounted) == 109


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


def test_batch_six_analytics_query_has_explicit_anonymous_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证统计查询契约明确声明匿名指标与维度。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    schema = _openapi(monkeypatch)
    operation = schema["paths"]["/api/v1/admin/analytics"]["get"]
    assert {"period", "start", "end"} <= _parameter_names(operation)
    properties = set(
        _walk_property_names(_response_schema(operation), schema["components"]["schemas"])
    )
    assert {
        "period",
        "timezone",
        "rows",
        "ratios",
        "numerator",
        "denominator",
        "rate",
        "basis",
    } <= properties
    assert {"user_id", "wechat_id", "openid", "nickname", "trajectory"}.isdisjoint(properties)


def _openapi(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # 功能:替换运行时配置并生成管理 API 的 OpenAPI 契约。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:实际应用生成的 OpenAPI 字典。
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "test-access-secret")
    settings = Settings(
        database_url=SecretStr("mysql+pymysql://root:test@127.0.0.1/juya_admin_test"),
        redis_url=SecretStr("redis://127.0.0.1:6379/15"),
        internal_hmac_secret=SecretStr("test-internal-secret"),
        oss_region="oss-cn-test",
        oss_bucket="juya-test",
        oss_expected_bucket="juya-test",
    )
    return create_app(settings).openapi()


def _parameter_names(operation: Mapping[str, Any]) -> set[str]:
    # 功能:提取 OpenAPI 操作声明的参数名集合。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    # 返回:操作参数名称集合。
    return {str(parameter["name"]).lower() for parameter in operation.get("parameters", [])}


def _response_schema(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    # 功能:读取 OpenAPI 成功响应的 JSON Schema。
    # 参数:
    #     operation: 待执行的业务命令名称,如开通、暂停、恢复或撤销。
    # 返回:成功响应的模型 Schema 字典。
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
    # 功能:递归展开 OpenAPI 属性及模型引用,避免循环遍历。
    # 参数:
    #     node: 需要遍历属性的 OpenAPI Schema 节点。
    #     components: OpenAPI 命名模型集合,供解析引用字段。
    #     visited: 已遍历模型名称集合,避免循环引用重复遍历。
    # 返回:逐个产生模型属性名的迭代器。
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
    # 功能:验证管理接口声明认证参数及安全响应头。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证敏感接口使用明确且安全的响应模型。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证第二批接口声明分页模型及写入认证。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证活动响应包含服务端计算的可执行操作。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    schema = _openapi(monkeypatch)
    components = schema["components"]["schemas"]
    for name in ("CampaignResponse", "CampaignListItemResponse"):
        assert "available_operations" in components[name]["properties"]
        assert "available_operations" in components[name]["required"]


def test_batch_three_feedback_routes_publish_aggregate_models_and_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 功能:验证反馈接口声明聚合响应模型及认证要求。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证内容接口声明编辑版本及写入认证。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
    # 功能:验证媒体接口声明任务模型及写入认证。
    # 参数:
    #     monkeypatch: pytest 提供的替换工具,用于临时修改环境、依赖或函数并自动恢复。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
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
