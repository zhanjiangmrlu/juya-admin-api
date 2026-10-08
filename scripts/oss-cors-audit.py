"""Inspect or append exact local origins on the explicitly bound private test bucket.

Default is read-only. --apply-local-test-rules preserves every existing rule.
Credentials are taken from an existing local container or environment, never logged.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

import alibabacloud_oss_v2 as oss
import httpx

from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider

ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:18173",
)
RULE_FIELDS = (
    "allowed_origins",
    "allowed_methods",
    "allowed_headers",
    "expose_headers",
    "max_age_seconds",
)


def assert_test_scope(settings, acl="private"):
    # 功能:确认操作范围为明确绑定的私有 juya-test 桶及本地或测试环境。
    # 参数:
    #     settings: 应用配置对象,供测试检查环境、桶绑定和凭证选项。
    #     acl: OSS 桶访问控制类型,审计仅允许 private。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    if (
        settings.environment not in {"local", "test"}
        or settings.oss_bucket != "juya-test"
        or settings.oss_expected_bucket != "juya-test"
        or acl != "private"
    ):
        raise RuntimeError("Only the explicitly bound private juya-test bucket is permitted")


def serialize(configuration):
    # 功能:将 SDK CORS 配置转换为可比较和输出的字典。
    # 参数:
    #     configuration: OSS CORS 配置对象,包含现有规则及 ResponseVary 设置。
    # 返回:含 rules 及 response_vary 的配置字典。
    return {
        "response_vary": configuration.response_vary,
        "rules": [
            {field: getattr(rule, field) for field in RULE_FIELDS}
            for rule in (configuration.cors_rules or [])
        ],
    }


def missing_origins(configuration):
    # 功能:按 OSS 首次匹配规则找出缺少完整跨域能力的本地来源。
    # 参数:
    #     configuration: OSS CORS 配置对象,包含现有规则及 ResponseVary 设置。
    # 返回:需要补充完整规则的本地来源列表。
    missing = []
    for origin in ORIGINS:
        for method in ("GET", "POST", "HEAD"):
            # Simple requests also use the first origin/method match. A complete
            # later rule cannot repair the exposed headers of an earlier match.
            rule = next(
                (
                    item
                    for item in (configuration.cors_rules or [])
                    # OSS only treats '*' as a wildcard; '?' and brackets stay literal.
                    if any(
                        re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), origin)
                        for pattern in (item.allowed_origins or [])
                    )
                    and method in (item.allowed_methods or [])
                ),
                None,
            )
            if (
                rule is None
                or not {"content-type", "range"}
                <= {header.lower() for header in (rule.allowed_headers or [])}
                or not {"etag", "x-oss-request-id"}
                <= {header.lower() for header in (rule.expose_headers or [])}
                or (rule.max_age_seconds or 0) < 600
            ):
                missing.append(origin)
                break
    return missing


def append_plan(configuration):
    # 功能:保留旧规则及顺序,为缺失来源前置准确的本地跨域规则。
    # 参数:
    #     configuration: OSS CORS 配置对象,包含现有规则及 ResponseVary 设置。
    # 返回:包含完整本地覆盖规则且保留原配置的 CORSConfiguration。
    missing = missing_origins(configuration)
    rules = list(configuration.cors_rules or [])
    if missing:
        if len(rules) >= 10:
            raise RuntimeError("Rule capacity reached; no existing rule will be replaced")
        # OSS uses its first matching rule, so place exact local overrides first.
        # Existing rules keep all fields and their relative order.
        rules.insert(
            0,
            oss.CORSRule(
                allowed_origins=missing,
                allowed_methods=["GET", "POST", "HEAD"],
                allowed_headers=["Content-Type", "Range"],
                expose_headers=["ETag", "x-oss-request-id"],
                max_age_seconds=600,
            ),
        )
    return oss.CORSConfiguration(cors_rules=rules, response_vary=configuration.response_vary)


def assert_existing_rules_preserved(before, after):
    # 功能:检查原 CORS 规则内容、相对顺序和 ResponseVary 均被保留。
    # 参数:
    #     before: 应用计划前的 CORS 配置快照。
    #     after: 应用计划后读取并序列化的 CORS 配置快照。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    if after["response_vary"] != before["response_vary"]:
        raise RuntimeError("Existing CORS ResponseVary changed")
    remaining = iter(after["rules"])
    for old in before["rules"]:
        if not any(new == old for new in remaining):
            raise RuntimeError("Existing CORS rules were changed, removed, or reordered")


def load_provider(container):
    # 功能:从可选容器环境加载 OSS 配置并创建受测试桶约束的适配器。
    # 参数:
    #     container: 可选本地容器名称,用于读取该容器的 OSS 环境配置。
    # 返回:已检查的 Settings 与 OSS 适配器二元组。
    if container:
        info = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
        values = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        for key, value in values.items():
            if key.startswith(("OSS_", "JUYA_OSS_")):
                os.environ[key] = value
        os.environ["JUYA_ENVIRONMENT"] = values.get("JUYA_ENVIRONMENT", "")
    settings = Settings()
    assert_test_scope(settings)
    settings.validate_oss_configuration()
    return settings, AliyunOssProvider(
        settings.oss_region,
        settings.oss_bucket,
        endpoint=settings.oss_endpoint,
        credentials_provider=ControlledCredentialsProvider(),
    )


def preflights(provider):
    # 功能:对本地和非可信来源发起 OPTIONS 请求并收集跨域响应事实。
    # 参数:
    #     provider: 测试资源操作使用的 OSS 适配器。
    # 返回:各来源和方法的预检响应状态及跨域头记录列表。
    rows = []
    with httpx.Client(timeout=20) as client:
        for origin in (*ORIGINS, "https://untrusted.example"):
            for method in ("POST", "GET", "HEAD"):
                response = client.options(
                    provider._bucket_url() + "/oss-live-tests/cors-probe",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": "content-type,range",
                    },
                )
                rows.append(
                    {
                        "origin": origin,
                        "method": method,
                        "status": response.status_code,
                        "allow_origin": response.headers.get("access-control-allow-origin"),
                        "allow_headers": response.headers.get("access-control-allow-headers"),
                        "allow_methods": response.headers.get("access-control-allow-methods"),
                        "expose_headers": response.headers.get("access-control-expose-headers"),
                        "max_age": response.headers.get("access-control-max-age"),
                    }
                )
    return rows


def main():
    # 功能:审计测试桶跨域配置,可按显式参数应用本地规则并输出检查报告。
    # 参数:无。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="juya-admin-api-admin-api-1")
    parser.add_argument("--apply-local-test-rules", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    settings, provider = load_provider(args.container)
    acl = provider._client.get_bucket_acl(oss.GetBucketAclRequest(bucket=settings.oss_bucket)).acl
    assert_test_scope(settings, acl)
    before = provider._client.get_bucket_cors(
        oss.GetBucketCorsRequest(bucket=settings.oss_bucket)
    ).cors_configuration
    plan = append_plan(before)
    before_data, plan_data = serialize(before), serialize(plan)
    report = {
        "bucket": settings.oss_bucket,
        "expected_bucket": settings.oss_expected_bucket,
        "environment": settings.environment,
        "acl": acl,
        "before": before_data,
        "planned": plan_data,
        "applied": False,
    }
    if args.apply_local_test_rules and before_data != plan_data:
        fresh = provider._client.get_bucket_cors(
            oss.GetBucketCorsRequest(bucket=settings.oss_bucket)
        ).cors_configuration
        if serialize(fresh) != before_data:
            raise RuntimeError("CORS changed during planning; no update made")
        provider._client.put_bucket_cors(
            oss.PutBucketCorsRequest(bucket=settings.oss_bucket, cors_configuration=plan)
        )
        report["applied"] = True
    after = provider._client.get_bucket_cors(
        oss.GetBucketCorsRequest(bucket=settings.oss_bucket)
    ).cors_configuration
    report["after"] = serialize(after)
    assert_existing_rules_preserved(before_data, report["after"])
    report["existing_rules_preserved"] = True
    report["preflight"] = preflights(provider)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SDK/HTTP exception strings can contain Authorization or signed URLs.
        for _ in range(5):
            unwrap = getattr(error, "unwrap", None)
            if not callable(unwrap):
                break
            error = unwrap()
        code = str(getattr(error, "code", ""))
        print(
            json.dumps(
                {
                    "failed": True,
                    "error_type": type(error).__name__,
                    "code": code if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else None,
                }
            )
        )
        raise SystemExit(1) from None
