"""Offline guards for the live CORS operator; these tests never contact OSS."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import alibabacloud_oss_v2 as oss
import pytest

spec = importlib.util.spec_from_file_location(
    "oss_cors_audit", Path(__file__).parents[2] / "scripts" / "oss-cors-audit.py"
)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.mark.parametrize(
    "environment,bucket,expected,acl",
    [
        ("production", "juya-test", "juya-test", "private"),
        ("local", "juya-production", "juya-production", "private"),
        ("local", "juya-test", "juya-other", "private"),
        ("local", "juya-test", "juya-test", "public-read"),
    ],
)
def test_scope_rejects_production_mismatch_and_public_acl(environment, bucket, expected, acl):
    # 功能:验证 OSS 审计范围拒绝生产环境、桶绑定不匹配及公开 ACL。
    # 参数:
    #     environment: 参数化测试选择的运行环境,例如 local、test 或 production。
    #     bucket: OSS 测试桶名称,用于检查明确的环境绑定。
    #     expected: 参数化测试提供的预期结果,用于与实际返回值比较。
    #     acl: OSS 桶访问控制类型,审计仅允许 private。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    with pytest.raises(RuntimeError):
        audit.assert_test_scope(
            SimpleNamespace(
                environment=environment, oss_bucket=bucket, oss_expected_bucket=expected
            ),
            acl,
        )


def test_append_preserves_existing_rule_and_only_adds_missing_exact_origin():
    # 功能:验证新增规则保留旧规则且只补缺失的准确来源。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    old = oss.CORSRule(
        allowed_origins=["https://existing.example", audit.ORIGINS[0]],
        allowed_methods=["GET", "POST", "HEAD"],
        allowed_headers=["Content-Type", "Range"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[old], response_vary=True)
    plan = audit.append_plan(config)
    assert plan.cors_rules[1] is old and plan.response_vary is True
    assert plan.cors_rules[0].allowed_origins == list(audit.ORIGINS[1:])
    assert plan.cors_rules[0].allowed_headers == ["Content-Type", "Range"]
    assert plan.cors_rules[0].expose_headers == ["ETag", "x-oss-request-id"]
    assert plan.cors_rules[0].max_age_seconds == 600
    assert set(plan.cors_rules[0].allowed_methods) == {"POST", "GET", "HEAD"}
    assert len(config.cors_rules) == 1
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)


def test_local_origins_include_both_primary_hostnames_and_isolated_frontend():
    # 功能:验证本地来源包含两个主机名及独立前端地址。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    plan = audit.append_plan(oss.CORSConfiguration(cors_rules=[]))
    assert set(plan.cors_rules[0].allowed_origins) == {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:18173",
    }


@pytest.mark.parametrize(
    "headers,exposed,max_age",
    [
        (["content-type"], ["ETag", "x-oss-request-id"], 600),
        (["content-type", "range"], ["ETag"], 600),
        (["content-type", "range"], ["ETag", "x-oss-request-id"], 300),
    ],
)
def test_existing_partial_rule_gets_complete_rule_without_being_changed(headers, exposed, max_age):
    # 功能:验证不完整旧规则得到完整覆盖规则且旧规则内容不变。
    # 参数:
    #     headers: 模拟 CORS 请求或响应的 HTTP 头。
    #     exposed: CORS 暴露给浏览器的响应头名称集合。
    #     max_age: CORS 预检缓存时间,单位为秒。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    old = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "POST", "HEAD"],
        allowed_headers=headers,
        expose_headers=exposed,
        max_age_seconds=max_age,
    )
    config = oss.CORSConfiguration(cors_rules=[old], response_vary=False)
    before = audit.serialize(config)
    plan = audit.append_plan(config)
    assert len(plan.cors_rules) == 2
    assert audit.serialize(config) == before
    assert audit.serialize(plan)["rules"][1] == before["rules"][0]
    assert plan.response_vary is False
    assert plan.cors_rules[0].allowed_origins == list(audit.ORIGINS)
    assert plan.cors_rules[0].allowed_headers == ["Content-Type", "Range"]
    assert plan.cors_rules[0].expose_headers == ["ETag", "x-oss-request-id"]
    assert plan.cors_rules[0].max_age_seconds == 600
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)


@pytest.mark.parametrize("method,headers", [("GET", []), ("HEAD", []), ("POST", ["content-type"])])
def test_first_matching_rule_serves_complete_local_headers(method, headers):
    # 功能:验证首次匹配的规则提供完整本地响应头。
    # 参数:
    #     method: CORS 或签名请求所用 HTTP 方法。
    #     headers: 模拟 CORS 请求或响应的 HTTP 头。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    old = oss.CORSRule(
        allowed_origins=["http://127.0.0.1:5173", "https://existing.example"],
        allowed_methods=["GET", "POST", "HEAD"],
        allowed_headers=["content-type"],
        expose_headers=["ETag"],
        max_age_seconds=300,
    )
    unrelated = oss.CORSRule(allowed_origins=["https://other.example"], allowed_methods=["GET"])
    config = oss.CORSConfiguration(cors_rules=[old, unrelated], response_vary=True)
    plan = audit.append_plan(config)
    # OSS selects the first matching rule, including simple GET/HEAD with no custom headers.
    matched = next(
        rule
        for rule in plan.cors_rules
        if "http://127.0.0.1:5173" in rule.allowed_origins
        and method in rule.allowed_methods
        and set(headers) <= {header.lower() for header in rule.allowed_headers}
    )
    assert matched.expose_headers == ["ETag", "x-oss-request-id"]
    assert matched.max_age_seconds == 600
    assert audit.serialize(plan)["rules"][1:] == audit.serialize(config)["rules"]
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)


def test_post_apply_validation_accepts_preserved_rules_after_new_local_rule():
    # 功能:验证应用后的检查接受保留旧规则并新增本地规则。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    before = {"response_vary": True, "rules": [{"origin": "a"}, {"origin": "b"}]}
    after = {
        "response_vary": True,
        "rules": [{"origin": "local"}, {"origin": "a"}, {"origin": "b"}],
    }
    audit.assert_existing_rules_preserved(before, after)


def test_later_complete_rule_cannot_hide_earlier_partial_rule():
    # 功能:验证后续完整规则不能掩盖更早的不完整规则。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    partial = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["content-type"],
        expose_headers=["ETag"],
        max_age_seconds=300,
    )
    complete = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["Content-Type", "Range"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[partial, complete])
    plan = audit.append_plan(config)
    assert len(plan.cors_rules) == 3
    assert plan.cors_rules[0].max_age_seconds == 600
    assert audit.serialize(plan)["rules"][1:] == audit.serialize(config)["rules"]
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)


@pytest.mark.parametrize(
    "pattern,expected_origins",
    [
        (
            "*",
            ["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:18173"],
        ),
        ("http://127.0.0.1:*", ["http://127.0.0.1:5173", "http://127.0.0.1:18173"]),
        ("http://*:5173", ["http://127.0.0.1:5173", "http://localhost:5173"]),
    ],
)
def test_earlier_partial_wildcard_rule_requires_exact_local_override(pattern, expected_origins):
    # 功能:验证更早的不完整通配规则需要准确本地覆盖规则。
    # 参数:
    #     pattern: CORS 来源匹配模式,只有星号作为通配符。
    #     expected_origins: 预期需要补充规则的准确来源列表。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    partial = oss.CORSRule(
        allowed_origins=[pattern],
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["content-type"],
        expose_headers=["ETag"],
        max_age_seconds=300,
    )
    complete = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["Content-Type", "Range"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[partial, complete], response_vary=True)
    plan = audit.append_plan(config)
    assert len(plan.cors_rules) == 3
    assert plan.cors_rules[0].allowed_origins == expected_origins
    assert plan.cors_rules[0].expose_headers == ["ETag", "x-oss-request-id"]
    assert plan.cors_rules[0].max_age_seconds == 600
    assert audit.serialize(plan)["rules"][1:] == audit.serialize(config)["rules"]
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)
    at_capacity = oss.CORSConfiguration(cors_rules=[partial] + [complete] * 9)
    with pytest.raises(RuntimeError):
        audit.append_plan(at_capacity)


@pytest.mark.parametrize("pattern", ["http://local?ost:5173", "http://local[h]ost:5173"])
def test_origin_question_marks_and_brackets_are_literals(pattern):
    # 功能:验证来源中的问号及方括号按字面匹配。
    # 参数:
    #     pattern: CORS 来源匹配模式,只有星号作为通配符。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    partial = oss.CORSRule(
        allowed_origins=[pattern],
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["content-type"],
        expose_headers=["ETag"],
        max_age_seconds=300,
    )
    complete = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["Content-Type", "Range"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[partial, complete])
    assert audit.serialize(audit.append_plan(config)) == audit.serialize(config)


def test_complete_wildcard_rule_does_not_need_new_local_override():
    # 功能:验证完整通配规则无需新增本地覆盖规则。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    complete = oss.CORSRule(
        allowed_origins=["*"],
        allowed_methods=["GET", "HEAD", "POST"],
        allowed_headers=["Content-Type", "Range"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[complete])
    assert audit.serialize(audit.append_plan(config)) == audit.serialize(config)


@pytest.mark.parametrize(
    "after",
    [
        {"response_vary": True, "rules": [{"origin": "a"}]},
        {"response_vary": True, "rules": [{"origin": "b"}, {"origin": "a"}]},
        {"response_vary": True, "rules": [{"origin": "a"}, {"origin": "changed"}]},
        {"response_vary": False, "rules": [{"origin": "a"}, {"origin": "b"}]},
    ],
)
def test_post_apply_validation_rejects_removed_changed_reordered_rules_or_vary(after):
    # 功能:验证应用后拒绝删除、修改、重排旧规则或更改 Vary。
    # 参数:
    #     after: 应用计划后读取并序列化的 CORS 配置快照。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    before = {"response_vary": True, "rules": [{"origin": "a"}, {"origin": "b"}]}
    with pytest.raises(RuntimeError):
        audit.assert_existing_rules_preserved(before, after)


def test_complete_rules_at_capacity_need_no_replacement():
    # 功能:验证规则已满但现有规则完整时无需替换。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    old = oss.CORSRule(
        allowed_origins=list(audit.ORIGINS),
        allowed_methods=["GET", "POST", "HEAD"],
        allowed_headers=["CONTENT-TYPE", "RANGE"],
        expose_headers=["etag", "X-OSS-REQUEST-ID"],
        max_age_seconds=600,
    )
    config = oss.CORSConfiguration(cors_rules=[old] * 10, response_vary=True)
    assert audit.serialize(audit.append_plan(config)) == audit.serialize(config)


def test_rule_capacity_never_replaces_an_existing_rule():
    # 功能:验证规则容量已满时绝不替换已有规则。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    config = oss.CORSConfiguration(
        cors_rules=[oss.CORSRule(allowed_origins=["https://a.test"])] * 10
    )
    with pytest.raises(RuntimeError):
        audit.append_plan(config)
