"""Read-only browser CORS verification using an existing synthetic test-bucket object."""

import argparse
import importlib.util
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import alibabacloud_oss_v2 as oss

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "docs/implementation/v13-content/evidence/media-http.json"
RUNNER = Path(__file__).with_name("oss-cors-browser-runner.cjs")


def skip(reason: str) -> None:
    # 功能:输出结构化跳过原因并以跳过状态退出浏览器检查。
    # 参数:
    #     reason: 跳过浏览器检查的原因,输出为结构化诊断。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    print(json.dumps({"skipped": reason}))
    raise SystemExit(77)


def main() -> None:
    # 功能:验证依赖及现有 OSS 测试资源,再运行只读浏览器跨域检查。
    # 参数:无。
    # 返回:无, 通过输出、进程退出状态或异常报告检查结果。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--container", default="juya-admin-api-admin-api-1")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.fixture.is_file():
        skip("Synthetic fixture evidence missing; no object will be uploaded")
    node = shutil.which("node")
    if node is None:
        skip("Node.js is unavailable")
    dependency_check = subprocess.run(
        [node, str(RUNNER), "--check-dependencies"],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
    )
    if dependency_check.returncode:
        skip("Sibling juya-admin Playwright dependencies or Chromium are unavailable")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))["audio"]
    key = fixture["object_key"]
    if not isinstance(key, str) or not (
        key.startswith("uploads/audio/") and "/fixtures/v13-http/" in key
    ):
        raise ValueError("Only the existing synthetic fixture key is permitted")
    spec = importlib.util.spec_from_file_location(
        "oss_cors_audit", Path(__file__).with_name("oss-cors-audit.py")
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    settings, provider = audit.load_provider(args.container)
    get_url = provider._client.presign(
        oss.GetObjectRequest(bucket=settings.oss_bucket, key=key),
        expires=timedelta(seconds=120),
    ).url
    head_url = provider._client.presign(
        oss.HeadObjectRequest(
            bucket=settings.oss_bucket,
            key=key,
            headers={"Content-Type": "application/octet-stream"},
        ),
        expires=timedelta(seconds=120),
    ).url
    # Short-lived signatures are passed through stdin only, never through argv or files.
    child = subprocess.run(
        [node, str(RUNNER)],
        input=json.dumps(
            {
                "origins": list(audit.ORIGINS),
                "getUrl": get_url,
                "headUrl": head_url,
                "sha256": fixture["sha256"],
            }
        ).encode(),
        capture_output=True,
        cwd=ROOT,
        timeout=90,
    )
    if child.returncode:
        # Browser exceptions may include URLs; discard all raw child diagnostics.
        raise RuntimeError("Browser runner failed")
    report = json.loads(child.stdout.decode("utf-8"))
    report["bucket"] = settings.oss_bucket
    report["fixture_object_key"] = key
    report["post"] = "No upload performed; POST was checked by HTTP OPTIONS only."
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Neither credential/provider errors nor signed URLs may leak in tracebacks.
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
