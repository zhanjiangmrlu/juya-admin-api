"""功能:验证真实发布 CLI、环境隔离、包安全和批量发布控制流。

约定接口(主对话负责实现):
  common.load_config(path, environment, workspace) -> dict
  common.validate_bundle(archive, expected_sha256, project, environment, destination) -> dict
  publish.resolve_projects('all') -> list[str]
  publish.run_release(config, projects, environment, action, workspace, build_only=False) -> int
  CLI: --project/--environment/--action/--config/--workspace/--build-only
配置测试夹具使用 schema_version=1,environments.<env>.ssh/projects。
只替换外部命令边界; tar、配置、CLI 解析及发布分支均运行真实实现。
禁止通过源码字符串断言来模拟部署验收。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

from tests.unit.deploy.local_release_testkit import (
    PROJECTS,
    initialize_git,
    make_workspace,
    release_module,
    run_cli,
    write_bundle,
    write_config,
)


@pytest.mark.parametrize("project", ["all", *PROJECTS])
def test_cli_plan_selects_requested_project_without_building(tmp_path: Path, project: str) -> None:
    # 回归:漏掉项目映射或 plan 误执行构建会导致出口错误或出现制品。
    make_workspace(tmp_path)
    result = run_cli(
        tmp_path,
        write_config(tmp_path),
        "--project",
        project,
        "--environment",
        "test",
        "--action",
        "plan",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = PROJECTS if project == "all" else (project,)
    assert all(name in result.stdout for name in expected)
    assert not list(tmp_path.rglob("*.tar.gz"))
    assert not list(tmp_path.rglob("*.tgz"))
    assert not list(tmp_path.rglob("dist"))


def test_cli_rejects_unknown_arguments(tmp_path: Path) -> None:
    result = run_cli(tmp_path, write_config(tmp_path), "--not-a-release-option")
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


@pytest.mark.parametrize(
    "option,value",
    [
        ("--project", "juya-miniapp-weixin"),
        ("--environment", "staging"),
        ("--action", "delete"),
    ],
)
def test_cli_rejects_unknown_selection(tmp_path: Path, option: str, value: str) -> None:
    result = run_cli(tmp_path, write_config(tmp_path), option, value)
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_cli_missing_production_never_uses_test_profile(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    result = run_cli(
        tmp_path,
        write_config(tmp_path),
        "--project",
        "juya-admin",
        "--environment",
        "production",
        "--action",
        "plan",
    )
    assert result.returncode != 0
    assert "test.example.invalid" not in result.stdout
    assert not list(tmp_path.rglob("dist"))


def test_config_requires_explicit_production(tmp_path: Path) -> None:
    common = release_module("common")
    with pytest.raises((ValueError, RuntimeError)):
        common.load_config(write_config(tmp_path), "production", tmp_path)


@pytest.mark.parametrize("collision", ["root", "compose_project"])
def test_config_rejects_shared_test_and_production_target(tmp_path: Path, collision: str) -> None:
    common = release_module("common")
    config_path = write_config(tmp_path, production=True)
    config = json.loads(config_path.read_text())
    project = "juya-admin-api"
    profiles = config["environments"]
    profiles["production"]["projects"][project][collision] = profiles["test"]["projects"][project][
        collision
    ]
    # 同一主机的目录/Compose 项目不能被两个环境复用。
    profiles["production"]["ssh"]["host"] = profiles["test"]["ssh"]["host"]
    config_path.write_text(json.dumps(config))
    with pytest.raises((ValueError, RuntimeError)):
        common.load_config(config_path, "production", tmp_path)


@pytest.mark.parametrize("collision", ["root", "compose_project", "compose_env_file"])
def test_config_rejects_production_overlapping_a_different_test_project(
    tmp_path: Path,
    collision: str,
) -> None:
    # 回归:按同名项目比较会漏掉 production miniapp 覆盖 test admin 的情况。
    common = release_module("common")
    config_path = write_config(tmp_path, production=True)
    config = json.loads(config_path.read_text())
    profiles = config["environments"]
    profiles["production"]["ssh"]["host"] = profiles["test"]["ssh"]["host"]
    profiles["production"]["projects"]["juya-miniapp-api"][collision] = profiles["test"][
        "projects"
    ]["juya-admin-api"][collision]
    config_path.write_text(json.dumps(config))
    with pytest.raises((ValueError, RuntimeError)):
        common.load_config(config_path, "production", tmp_path)


def test_bundle_validates_and_extracts_expected_release(tmp_path: Path) -> None:
    common = release_module("common")
    archive = tmp_path / "release.tar.gz"
    digest = write_bundle(archive)
    destination = tmp_path / "unpacked"
    manifest = common.validate_bundle(archive, digest, "juya-admin", "test", destination)
    assert manifest["project"] == "juya-admin"
    assert manifest["environment"] == "test"
    assert (destination / "site" / "index.html").read_text() == "<html>fixture release</html>"


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape.txt",
        "/absolute.txt",
        "site/../../escape.txt",
        "C:/absolute.txt",
        "site\\..\\..\\escape.txt",
    ],
)
def test_bundle_rejects_unsafe_member_before_extraction(tmp_path: Path, unsafe_name: str) -> None:
    common = release_module("common")
    archive = tmp_path / "unsafe.tar.gz"
    digest = write_bundle(archive, entries={unsafe_name: b"must not be written"})
    destination = tmp_path / "unpacked"
    with pytest.raises((ValueError, RuntimeError)):
        common.validate_bundle(archive, digest, "juya-admin", "test", destination)
    assert not (tmp_path / "escape.txt").exists()
    assert not (destination / "site" / "index.html").exists()


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_bundle_rejects_links_before_extraction(tmp_path: Path, link_type: bytes) -> None:
    common = release_module("common")
    archive = tmp_path / "linked.tar.gz"
    digest = write_bundle(archive, link=("site/linked", "../escape", link_type))
    destination = tmp_path / "unpacked"
    with pytest.raises((ValueError, RuntimeError)):
        common.validate_bundle(archive, digest, "juya-admin", "test", destination)
    assert not (destination / "site" / "index.html").exists()


@pytest.mark.parametrize(
    "project,environment",
    [
        ("juya-miniapp-h5", "test"),
        ("juya-admin", "production"),
    ],
)
def test_bundle_rejects_manifest_target_mismatch(
    tmp_path: Path,
    project: str,
    environment: str,
) -> None:
    common = release_module("common")
    archive = tmp_path / "mismatched.tar.gz"
    digest = write_bundle(archive, project=project, environment=environment)
    with pytest.raises((ValueError, RuntimeError)):
        common.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "unpacked")


def test_bundle_rejects_corrupted_archive_sha256(tmp_path: Path) -> None:
    common = release_module("common")
    archive = tmp_path / "corrupted.tar.gz"
    digest = write_bundle(archive)
    archive.write_bytes(archive.read_bytes() + b"tampering")
    with pytest.raises((ValueError, RuntimeError)):
        common.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "unpacked")


def test_bundle_rejects_missing_internal_checksums(tmp_path: Path) -> None:
    common = release_module("common")
    archive = tmp_path / "unchecked.tar.gz"
    digest = write_bundle(archive, checksums=False)
    with pytest.raises((ValueError, RuntimeError)):
        common.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "unpacked")


def test_cli_production_dirty_repository_is_rejected_before_build(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    initialize_git(tmp_path, dirty=True, branch="main")
    result = run_cli(
        tmp_path,
        write_config(tmp_path, production=True),
        "--project",
        "juya-admin",
        "--environment",
        "production",
        "--action",
        "publish",
        "--build-only",
    )
    assert result.returncode != 0
    assert any(word in (result.stdout + result.stderr).lower() for word in ("dirty", "uncommitted"))
    assert not list(tmp_path.rglob("dist"))
    assert not list(tmp_path.rglob("*.tar.gz"))
    assert not list(tmp_path.rglob("*.tgz"))


def test_plan_performs_no_external_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish = release_module("publish")
    common = release_module("common")
    make_workspace(tmp_path)
    config = common.load_config(write_config(tmp_path), "test", tmp_path)

    def forbid_process(*args: object, **kwargs: object) -> None:
        pytest.fail(f"plan attempted an external command: {args}")

    monkeypatch.setattr(subprocess, "run", forbid_process)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    assert publish.run_release(config, list(PROJECTS), "test", "plan", tmp_path) == 0
    assert not list(tmp_path.rglob("*.tar.gz"))


def install_release_boundaries(
    publish: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fail_stage: str | None = None,
    fail_project: str = "juya-miniapp-api",
) -> list[tuple[str, str]]:
    """功能:替换慢速构建和远程进程边界,保留真实发布协调控制流。"""
    events: list[tuple[str, str]] = []

    def run_checks(recipe: dict, repo: Path) -> None:
        project = recipe["project"]
        assert repo.name == ("juya-miniapp" if project == "juya-miniapp-h5" else project)
        events.append(("check", project))
        if fail_stage == "check" and project == fail_project:
            raise RuntimeError("fixture quality command failed (exit 7)")

    def build_bundle(
        project: str,
        environment: str,
        profile: dict,
        workspace: Path,
        output_root: Path,
    ) -> dict:
        assert environment == "test"
        assert profile["project"] == project
        assert profile["environment"] == "test"
        assert workspace == tmp_path
        events.append(("build", project))
        if fail_stage == "build" and project == fail_project:
            raise RuntimeError("fixture build command failed (exit 8)")
        output_root.mkdir(parents=True, exist_ok=True)
        bundle = output_root / f"{project}.tar.gz"
        digest = write_bundle(bundle, project=project, kind=profile["kind"])
        with tarfile.open(bundle) as archive:
            manifest = json.loads(archive.extractfile("release.json").read())
        return {"archive": bundle, "sha256": digest, "manifest": manifest, "profile": profile}

    def upload_and_deploy(bundle: dict | None, profile: dict, config: dict, action: str) -> None:
        if action == "rollback":
            assert bundle is None
            events.append(("rollback", profile["project"]))
            return
        assert action == "publish" and bundle is not None
        assert bundle["archive"].is_file()
        assert config["ssh"]["host"] == "test.example.invalid"
        events.append(("upload", profile["project"]))
        if fail_stage == "upload" and profile["project"] == fail_project:
            raise RuntimeError("fixture upload command failed (exit 9)")

    monkeypatch.setattr(publish, "run_checks", run_checks)
    monkeypatch.setattr(publish, "build_bundle", build_bundle)
    monkeypatch.setattr(publish, "upload_and_deploy", upload_and_deploy)
    # 预检和 HTTPS 检查位于实际外部系统边界,此处只验证发布阶段调度。
    monkeypatch.setattr(publish, "doctor", lambda config, projects, workspace: None)
    monkeypatch.setattr(publish, "public_health", lambda profile: None)
    return events


def test_batch_builds_every_project_before_first_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = release_module("publish")
    common = release_module("common")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    config = common.load_config(write_config(tmp_path), "test", tmp_path)
    events = install_release_boundaries(publish, monkeypatch, tmp_path)
    assert (
        publish.run_release(config, publish.resolve_projects("all"), "test", "publish", tmp_path)
        == 0
    )
    first_upload = next(index for index, event in enumerate(events) if event[0] == "upload")
    assert set(events[:first_upload]) == {
        (stage, project) for project in PROJECTS for stage in ("check", "build")
    }
    assert [project for stage, project in events if stage == "upload"] == list(PROJECTS)


@pytest.mark.parametrize("fail_stage", ["check", "build"])
def test_batch_quality_failure_stops_without_any_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_stage: str,
) -> None:
    publish = release_module("publish")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    write_config(tmp_path)
    events = install_release_boundaries(publish, monkeypatch, tmp_path, fail_stage=fail_stage)
    result = publish.main(
        [
            "--config",
            str(tmp_path / "release-config.json"),
            "--workspace",
            str(tmp_path),
            "--project",
            "all",
            "--environment",
            "test",
            "--action",
            "publish",
        ]
    )
    assert result != 0
    assert (fail_stage, "juya-miniapp-api") in events
    assert not any(stage == "upload" for stage, _ in events)
    assert events[-1] == (fail_stage, "juya-miniapp-api")


def test_batch_remote_failure_stops_later_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = release_module("publish")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    write_config(tmp_path)
    events = install_release_boundaries(publish, monkeypatch, tmp_path, fail_stage="upload")
    assert (
        publish.main(
            [
                "--config",
                str(tmp_path / "release-config.json"),
                "--workspace",
                str(tmp_path),
                "--project",
                "all",
                "--environment",
                "test",
                "--action",
                "publish",
            ]
        )
        != 0
    )
    assert [project for stage, project in events if stage == "upload"] == [
        "juya-admin-api",
        "juya-miniapp-api",
    ]


def test_build_only_runs_all_builds_without_remote_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = release_module("publish")
    common = release_module("common")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    config = common.load_config(write_config(tmp_path), "test", tmp_path)
    events = install_release_boundaries(publish, monkeypatch, tmp_path)
    assert (
        publish.run_release(
            config,
            list(PROJECTS),
            "test",
            "publish",
            tmp_path,
            build_only=True,
        )
        == 0
    )
    assert {project for stage, project in events if stage == "build"} == set(PROJECTS)
    assert not any(stage == "upload" for stage, _ in events)


def test_cli_real_build_only_keeps_credentials_out_of_archive_and_logs(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    config = write_config(tmp_path)
    credential = "FAKE_BUSINESS_PASSWORD_FOR_RELEASE_TEST_ONLY"
    (tmp_path / "juya-admin" / ".env").write_text(f"SECRET={credential}\n")
    initialize_git(tmp_path)
    result = run_cli(
        tmp_path,
        config,
        "--project",
        "juya-admin",
        "--environment",
        "test",
        "--action",
        "publish",
        "--build-only",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = [*tmp_path.rglob("*.tar.gz"), *tmp_path.rglob("*.tgz")]
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        names = archive.getnames()
        assert "release.json" in names
        assert "SHA256SUMS" in names
        assert "site/index.html" in names
        assert not any(name.endswith((".pem", ".env", "release-config.json")) for name in names)
        contents = b"".join(
            archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()
        )
    assert credential.encode() not in contents
    assert b"FAKE_PRIVATE_KEY_FOR_RELEASE_TEST_ONLY" not in contents
    assert credential not in result.stdout + result.stderr
    assert "FAKE_PRIVATE_KEY_FOR_RELEASE_TEST_ONLY" not in result.stdout + result.stderr
    for logfile in (tmp_path / ".local-release").rglob("*.log"):
        assert credential not in logfile.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt])
def test_public_health_failure_or_interrupt_restores_previous_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    publish = release_module("publish")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    write_config(tmp_path)
    events = install_release_boundaries(publish, monkeypatch, tmp_path)

    def fail_health(profile: dict) -> None:
        raise failure("fixture health check interrupted")

    monkeypatch.setattr(publish, "public_health", fail_health)
    result = publish.main(
        [
            "--config",
            str(tmp_path / "release-config.json"),
            "--workspace",
            str(tmp_path),
            "--project",
            "all",
            "--environment",
            "test",
            "--action",
            "publish",
        ]
    )
    assert result != 0
    assert [(stage, project) for stage, project in events if stage in ("upload", "rollback")] == [
        ("upload", "juya-admin-api"),
        ("rollback", "juya-admin-api"),
    ]


def test_cli_build_rejects_site_symlink_to_local_credentials(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    config = write_config(tmp_path)
    assets = tmp_path / "juya-admin" / "dist" / "assets"
    assets.mkdir(parents=True)
    try:
        (assets / "credential.bin").symlink_to(tmp_path / "fixture-identity.pem")
    except OSError as error:
        pytest.skip(f"this platform cannot create a real symlink: {error}")
    initialize_git(tmp_path)
    result = run_cli(
        tmp_path,
        config,
        "--project",
        "juya-admin",
        "--environment",
        "test",
        "--action",
        "publish",
        "--build-only",
    )
    # copytree 若先跟随链接,staging 中的链接检查已无法挡住凭据内容。
    for bundle in tmp_path.rglob("*.tgz"):
        with tarfile.open(bundle) as archive:
            contents = b"".join(
                archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile()
            )
        assert b"FAKE_PRIVATE_KEY_FOR_RELEASE_TEST_ONLY" not in contents
    assert result.returncode != 0, "site symlink was accepted and could copy a local credential"
    assert not list(tmp_path.rglob("*.tgz"))


def remote_args(
    profile_path: Path, archive: Path, digest: str, action: str = "publish"
) -> list[str]:
    """功能:生成显式目标的远程 CLI 参数,不会调用真实服务器。"""
    return [
        "--profile",
        str(profile_path),
        "--project",
        "juya-admin",
        "--environment",
        "test",
        "--action",
        action,
        "--bundle",
        str(archive),
        "--sha256",
        digest,
    ]


def test_remote_dispatches_only_validated_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = release_module("remote")
    config_path = write_config(tmp_path)
    profile = json.loads(config_path.read_text())["environments"]["test"]["projects"]["juya-admin"]
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    archive = tmp_path / "remote.tgz"
    digest = write_bundle(archive)
    marker = tmp_path / "handler-result.txt"

    def deploy_web(bundle: Path, profile: dict, manifest: dict, action: str) -> dict:
        assert action == "publish"
        assert manifest["project"] == profile["project"] == "juya-admin"
        assert manifest["environment"] == profile["environment"] == "test"
        marker.write_text((bundle / "site" / "index.html").read_text())
        return {"status": "published"}

    handler = types.ModuleType("web")
    handler.deploy_web = deploy_web
    monkeypatch.setitem(sys.modules, "web", handler)
    previous_mask = os.umask(0o077)
    try:
        assert remote.main(remote_args(profile_path, archive, digest)) == 0
    finally:
        os.umask(previous_mask)
    assert marker.read_text() == "<html>fixture release</html>"


@pytest.mark.parametrize("invalid", ["sha256", "manifest", "path", "kind", "profile"])
def test_remote_invalid_bundle_never_invokes_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    remote = release_module("remote")
    config_path = write_config(tmp_path)
    profile = json.loads(config_path.read_text())["environments"]["test"]["projects"]["juya-admin"]
    if invalid == "profile":
        profile["environment"] = "production"
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    archive = tmp_path / "invalid.tgz"
    digest = write_bundle(
        archive,
        environment="production" if invalid == "manifest" else "test",
        kind="api" if invalid == "kind" else "web",
        entries={"../escape.txt": b"escape"} if invalid == "path" else None,
    )
    if invalid == "sha256":
        digest = "0" * 64
    marker = tmp_path / "deployment-attempt.txt"

    def deploy_web(*args: object, **kwargs: object) -> dict:
        marker.write_text("deployment attempted before validation")
        return {"status": "published"}

    handler = types.ModuleType("web")
    handler.deploy_web = deploy_web
    monkeypatch.setitem(sys.modules, "web", handler)
    previous_mask = os.umask(0o077)
    try:
        assert remote.main(remote_args(profile_path, archive, digest)) != 0
    finally:
        os.umask(previous_mask)
    assert not marker.exists()
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize("action", ["rollback", "doctor", "plan"])
def test_build_only_with_nonpublish_action_never_performs_remote_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    publish = release_module("publish")
    make_workspace(tmp_path)
    config = write_config(tmp_path)
    operations: list[str] = []
    monkeypatch.setattr(publish, "doctor", lambda *args: operations.append("doctor"))
    monkeypatch.setattr(publish, "upload_and_deploy", lambda *args: operations.append("deploy"))
    monkeypatch.setattr(publish, "public_health", lambda *args: operations.append("health"))
    publish.main(
        [
            "--config",
            str(config),
            "--workspace",
            str(tmp_path),
            "--project",
            "all",
            "--environment",
            "test",
            "--action",
            action,
            "--build-only",
        ]
    )
    assert not operations, f"build-only action {action} performed {operations}"
