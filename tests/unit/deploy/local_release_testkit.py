"""功能:用独立临时仓库和真实 tar 包驱动本地发布行为测试。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

RELEASE_DIR = Path(__file__).resolve().parents[3] / "deploy" / "local-release"
PROJECTS = ("juya-admin-api", "juya-miniapp-api", "juya-admin", "juya-miniapp-h5")
REPOSITORIES = {
    "juya-admin-api": "juya-admin-api",
    "juya-miniapp-api": "juya-miniapp-api",
    "juya-admin": "juya-admin",
    "juya-miniapp-h5": "juya-miniapp",
}


def release_module(name: str) -> Any:
    """功能:在测试命名空间加载真实发布模块,缺少实现时给出明确失败。"""
    path = RELEASE_DIR / f"{name}.py"
    assert path.is_file(), f"Release implementation missing: {path}"
    sys.path.insert(0, str(RELEASE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"local_release_test_{name}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def write_config(workspace: Path, *, production: bool = False) -> Path:
    """功能:生成仅含虚构 SSH 目标的环境配置,不读取任何真实凭据。"""
    key_path = workspace / "fixture-identity.pem"
    key_path.write_text("FAKE_PRIVATE_KEY_FOR_RELEASE_TEST_ONLY")
    environments: dict[str, Any] = {}
    for environment in ("test", "production") if production else ("test",):
        profiles: dict[str, Any] = {}
        for project in PROJECTS:
            root = f"/opt/juya/{environment}/{project}"
            profile: dict[str, Any] = {
                "project": project,
                "environment": environment,
                "kind": "api" if project.endswith("api") else "web",
                "root": root,
                "health_url": f"https://{environment}-{project}.example.invalid/health/ready",
            }
            if project.endswith("api"):
                profile.update(
                    compose_project=f"{project}-{environment}",
                    compose_env_file=f"/etc/juya/{project}-{environment}.env",
                    compose_files=[f"docker-compose.ecs-{environment}.yml"],
                    services=["api", "worker", "beat"],
                    api_service="api",
                    worker_services=["worker"],
                    beat_services=["beat"],
                    migration_service=None,
                )
            else:
                profile.update(
                    site_path=f"{root}/current",
                    site_url=f"https://{environment}-{project}.example.invalid",
                    api_url=f"https://{environment}-api.example.invalid/health/ready",
                    expected_api_base=f"https://{environment}-api.example.invalid",
                    page_path="/",
                )
            profiles[project] = profile
        environments[environment] = {
            "ssh": {
                "host": f"{environment}.example.invalid",
                "user": "release",
                "port": 22,
                "identity_file": str(key_path),
            },
            "projects": profiles,
        }
    path = workspace / "release-config.json"
    path.write_text(json.dumps({"schema_version": 1, "environments": environments}))
    return path


def initialize_git(workspace: Path, *, dirty: bool = False, branch: str = "test") -> None:
    """功能:在临时仓库创建真实提交,可选留下业务改动用于生产门禁。"""
    for repository in REPOSITORIES.values():
        repo = workspace / repository
        for args in (
            ["init", "--quiet", f"--initial-branch={branch}"],
            ["add", "."],
            [
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        if dirty:
            (repo / "uncommitted-business-change.txt").write_text("must block production")


def make_workspace(workspace: Path) -> None:
    """功能:建立受控的四项目 recipe,所有构建命令只写临时输出。"""
    for project, repository in REPOSITORIES.items():
        repo = workspace / repository
        (repo / "deploy").mkdir(parents=True, exist_ok=True)
        (repo / ".gitignore").write_text("dist/\n.env\n*.pem\n")
        script = repo / "fixture-build.py"
        script.write_text(
            "from pathlib import Path\nimport os\n"
            "p=Path('dist'); (p/'assets').mkdir(parents=True,exist_ok=True)\n"
            "(p/'index.html').write_text('<script src=\"/assets/app.js\"></script>')\n"
            "(p/'assets'/'app.js').write_text(os.getenv('VITE_API_BASE_URL',"
            "'https://test-api.example.invalid'))\n"
        )
        recipe = {
            "schema_version": 1,
            "project": project,
            "repository": repository,
            "kind": "api" if project.endswith("api") else "web",
            "checks": [[sys.executable, "-c", "pass"]],
            "site_output": "dist",
            "build": {
                environment: {"command": [sys.executable, "fixture-build.py"], "env": {}}
                for environment in ("test", "production")
            },
            "dockerfile": "Dockerfile",
            "compose_files": {
                environment: [f"docker-compose.ecs-{environment}.yml"]
                for environment in ("test", "production")
            },
            "services": ["api", "worker", "beat"],
            "api_service": "api",
            "worker_services": ["worker"],
            "beat_services": ["beat"],
            "migration_service": None,
        }
        (repo / "deploy" / "local-release.json").write_text(json.dumps(recipe))
        for environment in ("test", "production"):
            (repo / "deploy" / f"docker-compose.ecs-{environment}.yml").write_text(
                "services:\n  api:\n    image: fixture-api:latest\n"
            )
        (repo / "Dockerfile").write_text("FROM scratch\n")


def run_cli(workspace: Path, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """功能:通过独立 Python 进程执行真实 CLI 并收集公开输出。"""
    return subprocess.run(
        [
            sys.executable,
            str(RELEASE_DIR / "publish.py"),
            "--config",
            str(config),
            "--workspace",
            str(workspace),
            *args,
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=20,
        check=False,
    )


def write_bundle(
    path: Path,
    *,
    project: str = "juya-admin",
    environment: str = "test",
    kind: str = "web",
    entries: dict[str, bytes] | None = None,
    link: tuple[str, str, bytes] | None = None,
    checksums: bool = True,
) -> str:
    """功能:手工生成 manifest/checksum 及可选恶意条目,返回包 SHA256。"""
    manifest = {
        "schema_version": 1,
        "project": project,
        "environment": environment,
        "release_id": "fixture-20261013-001",
        "commit": "a" * 40,
        "branch": "test" if environment == "test" else "main",
        "kind": kind,
    }
    files = {
        "release.json": json.dumps(manifest).encode(),
        "site/index.html": b"<html>fixture release</html>",
        **(entries or {}),
    }
    if checksums:
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in files.items()
        ).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        if link:
            name, target, kind = link
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = target
            archive.addfile(member)
    return hashlib.sha256(path.read_bytes()).hexdigest()
