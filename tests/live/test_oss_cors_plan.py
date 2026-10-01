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
    with pytest.raises(RuntimeError):
        audit.assert_test_scope(
            SimpleNamespace(
                environment=environment, oss_bucket=bucket, oss_expected_bucket=expected
            ),
            acl,
        )


def test_append_preserves_existing_rule_and_only_adds_missing_exact_origin():
    old = oss.CORSRule(
        allowed_origins=["https://existing.example", audit.ORIGINS[0]],
        allowed_methods=["GET", "POST", "HEAD"],
        allowed_headers=["content-type"],
        expose_headers=["ETag"],
        max_age_seconds=300,
    )
    config = oss.CORSConfiguration(cors_rules=[old], response_vary=True)
    plan = audit.append_plan(config)
    assert plan.cors_rules[0] is old and plan.response_vary is True
    assert plan.cors_rules[1].allowed_origins == [audit.ORIGINS[1]]
    assert plan.cors_rules[1].allowed_headers == ["content-type"]
    assert set(plan.cors_rules[1].allowed_methods) == {"POST", "GET", "HEAD"}
    assert len(config.cors_rules) == 1
    assert audit.serialize(audit.append_plan(plan)) == audit.serialize(plan)


def test_rule_capacity_never_replaces_an_existing_rule():
    config = oss.CORSConfiguration(
        cors_rules=[oss.CORSRule(allowed_origins=["https://a.test"])] * 10
    )
    with pytest.raises(RuntimeError):
        audit.append_plan(config)
