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

ORIGINS = ("http://127.0.0.1:5173", "http://127.0.0.1:18173")
RULE_FIELDS = (
    "allowed_origins",
    "allowed_methods",
    "allowed_headers",
    "expose_headers",
    "max_age_seconds",
)


def assert_test_scope(settings, acl="private"):
    if (
        settings.environment not in {"local", "test"}
        or settings.oss_bucket != "juya-test"
        or settings.oss_expected_bucket != "juya-test"
        or acl != "private"
    ):
        raise RuntimeError("Only the explicitly bound private juya-test bucket is permitted")


def serialize(configuration):
    return {
        "response_vary": configuration.response_vary,
        "rules": [
            {field: getattr(rule, field) for field in RULE_FIELDS}
            for rule in (configuration.cors_rules or [])
        ],
    }


def missing_origins(configuration):
    return [
        origin
        for origin in ORIGINS
        if not any(
            origin in (rule.allowed_origins or [])
            and {"POST", "GET", "HEAD"} <= set(rule.allowed_methods or [])
            and "content-type" in {header.lower() for header in (rule.allowed_headers or [])}
            for rule in (configuration.cors_rules or [])
        )
    ]


def append_plan(configuration):
    missing = missing_origins(configuration)
    rules = list(configuration.cors_rules or [])
    if missing:
        if len(rules) >= 10:
            raise RuntimeError("Rule capacity reached; no existing rule will be replaced")
        rules.append(
            oss.CORSRule(
                allowed_origins=missing,
                allowed_methods=["GET", "POST", "HEAD"],
                allowed_headers=["content-type"],
                expose_headers=["ETag"],
                max_age_seconds=300,
            )
        )
    return oss.CORSConfiguration(cors_rules=rules, response_vary=configuration.response_vary)


def load_provider(container):
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
    rows = []
    with httpx.Client(timeout=20) as client:
        for origin in (*ORIGINS, "https://untrusted.example"):
            for method in ("POST", "GET", "HEAD"):
                response = client.options(
                    provider._bucket_url() + "/oss-live-tests/cors-probe",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                rows.append(
                    {
                        "origin": origin,
                        "method": method,
                        "status": response.status_code,
                        "allow_origin": response.headers.get("access-control-allow-origin"),
                        "allow_headers": response.headers.get("access-control-allow-headers"),
                    }
                )
    return rows


def main():
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
    assert report["after"]["rules"][: len(before_data["rules"])] == before_data["rules"]
    assert report["after"]["response_vary"] == before_data["response_vary"]
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
