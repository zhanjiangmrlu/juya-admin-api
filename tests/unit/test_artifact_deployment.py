import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHA = "a" * 40
IMAGE = f"juya-admin-api-test:{SHA}"
IMAGE_ID = "sha256:" + "b" * 64


def posix_path(path: Path) -> str:
    # 参数 path 为本机路径,返回 Git Bash 或 Linux 可使用的绝对路径
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[str, Path, dict[str, str]]:
    # 参数 tmp_path 为 pytest 临时目录,返回隔离 Shell 发布夹具
    bash = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
    if not bash or not Path(bash).exists():
        pytest.skip("bash required")
    bundle = tmp_path / "bundle"
    (bundle / "deploy").mkdir(parents=True)
    for filename in ("docker-compose.ecs-2gb.yml", "docker-compose.ecs-test.yml"):
        (bundle / "deploy" / filename).write_text("services: {}\n", encoding="utf-8", newline="\n")
    (bundle / "image.tar.gz").write_bytes(b"test-image")
    (bundle / "image-ref.txt").write_text(IMAGE + "\n", encoding="utf-8", newline="\n")
    (bundle / "image-id.txt").write_text(IMAGE_ID + "\n", encoding="utf-8", newline="\n")
    manifest = []
    for file in sorted(bundle.rglob("*")):
        if file.is_file():
            digest = hashlib.sha256(file.read_bytes()).hexdigest()
            manifest.append(f"{digest}  {file.relative_to(bundle).as_posix()}")
    (bundle / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(
        "#!/bin/sh\n"
        'printf "%s|%s\\n" "${JUYA_ADMIN_API_IMAGE:-}" "$*" >> "$CALL_LOG"\n'
        'case "$*" in\n'
        '  version*) echo "Error response from daemon: flow not support" >&2; exit 1;;\n'
        '  "network create "*)\n'
        '    [ "${FAIL_NETWORK:-0}" != 1 ] || { echo network-unavailable >&2; exit 71; };;\n'
        '  "build "*)\n'
        '    [ "${FAIL_BUILD:-0}" != 1 ] || { echo build-failed >&2; exit 72; };;\n'
        '  "save --output "*) printf test-image > "$3";;\n'
        f'  *"image inspect"*) echo "{IMAGE_ID}";;\n'
        '  *"{{.Config.Image}}"*) echo "old-image:previous";;\n'
        '  *"{{.State.ExitCode}}"*) echo 0;;\n'
        '  *"{{.State.Status}}"*)\n'
        '    if [ "${FAIL_BACKGROUND:-0}" = 1 ]; then echo "restarting 1";\n'
        '    else echo "running 0"; fi;;\n'
        '  *"ps -q"*) echo old-container;;\n'
        '  *"mysqldump"*) echo database-backup;;\n'
        '  *"run --rm --no-deps migrate"*) [ "${FAIL_MIGRATION:-0}" != 1 ];;\n'
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    (binaries / "curl").write_text('#!/bin/sh\n[ "${FAIL_READY:-0}" != 1 ]\n', encoding="utf-8")
    (binaries / "flock").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    subprocess.run([bash, "-c", f"chmod +x '{posix_path(binaries)}'/*"], check=True)
    server = tmp_path / "server"
    (server / "deploy").mkdir(parents=True)
    (server / "deploy" / "docker-compose.ecs-2gb.yml").write_text("services: {}\n")
    env_file = tmp_path / "compose.env"
    env_file.write_text("JUYA_RUNTIME_ENV_FILE=/etc/juya/admin-api.env\n")
    environment = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", "/tmp"),
        "PATH": posix_path(binaries) + ":/usr/bin:/bin",
        "JUYA_TEST_PATH": posix_path(binaries) + ":/usr/bin:/bin",
        "MSYS": "winsymlinks:nativestrict",
        "CALL_LOG": posix_path(tmp_path / "calls"),
        "JUYA_DEPLOY_TEST_MODE": "1",
        "JUYA_DEPLOY_TEST_ROOT": posix_path(server),
        "JUYA_COMPOSE_ENV_FILE": posix_path(env_file),
    }
    return bash, bundle, environment


def run_deploy(
    deployment: tuple[str, Path, dict[str, str]], branch: str = "test", **flags: str
) -> subprocess.CompletedProcess[str]:
    # 参数 deployment 为隔离夹具,branch 为发布分支,flags 为模拟失败开关
    bash, bundle, environment = deployment
    return subprocess.run(
        [
            bash,
            "-c",
            'export PATH="$JUYA_TEST_PATH"; exec sh "$1" "$2" "$3"',
            "test-runner",
            posix_path(ROOT / "deploy/ecs-artifact-deploy.sh"),
            branch,
            posix_path(bundle),
        ],
        env={**environment, **flags},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_rejects_non_test_branch_before_docker(deployment, tmp_path: Path) -> None:
    # 参数 deployment 为发布夹具,tmp_path 为记录副作用的临时目录
    result = run_deploy(deployment, branch="main")
    assert result.returncode != 0
    assert "only test" in result.stderr
    assert not (tmp_path / "calls").exists()


def test_rejects_corrupt_artifact_before_stopping(deployment, tmp_path: Path) -> None:
    # 参数 deployment 为发布夹具,tmp_path 为记录副作用的临时目录
    (deployment[1] / "image.tar.gz").write_bytes(b"corrupted")
    result = run_deploy(deployment)
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


def test_success_keeps_volumes_and_backs_up_before_migration(deployment, tmp_path: Path) -> None:
    # 参数 deployment 为发布夹具,tmp_path 为检查备份和命令顺序的临时目录
    result = run_deploy(deployment)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert calls.index("mysqldump") < calls.index("run --rm --no-deps migrate")
    assert "down" not in calls and "--volumes" not in calls
    assert "up -d --no-deps admin-api admin-worker admin-beat" in calls
    assert (tmp_path / "server/current-release").is_symlink()
    assert list((tmp_path / "server/backups").glob("*.sql"))


@pytest.mark.parametrize("failure", ["FAIL_MIGRATION", "FAIL_READY", "FAIL_BACKGROUND"])
def test_failure_restores_previous_application(deployment, tmp_path: Path, failure: str) -> None:
    # 参数 deployment 为发布夹具,tmp_path 为命令记录目录,failure 为失败阶段
    result = run_deploy(deployment, **{failure: "1"})
    assert result.returncode != 0
    calls = (tmp_path / "calls").read_text()
    assert "old-image:previous|compose" in calls
    assert "up -d --no-deps admin-api admin-worker admin-beat" in calls
    assert not (tmp_path / "server/current-release").exists()


def run_ci_script(
    deployment: tuple[str, Path, dict[str, str]], script: str, **flags: str
) -> subprocess.CompletedProcess[str]:
    # 参数 deployment 为隔离夹具,script 为 CI 脚本,flags 为真实操作失败开关
    bash, bundle, environment = deployment
    arguments = ["test", SHA, "artifact"] if script == "build-artifact.sh" else []
    return subprocess.run(
        [
            bash,
            "-c",
            'export PATH="$JUYA_TEST_PATH"; exec sh "$@"',
            "test-runner",
            posix_path(ROOT / "deploy" / script),
            *arguments,
        ],
        cwd=bundle,
        env={**environment, "CI_COMMIT_REF_NAME": "test", **flags},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


@pytest.mark.parametrize("script", ["ci-verify.sh", "build-artifact.sh"])
def test_ci_scripts_continue_when_version_api_is_unsupported(
    deployment, tmp_path: Path, script: str
) -> None:
    # 云效版本查询失败不能阻止实际测试与构建;其他 Docker 操作由隔离夹具模拟
    result = run_ci_script(deployment, script)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    if script == "ci-verify.sh":
        assert "network create juya-ci-" in calls
        assert "ghcr.io/astral-sh/uv:python3.13-bookworm-slim" in calls
    else:
        assert "build --platform linux/amd64" in calls
        assert (deployment[1] / "artifact/image.tar.gz").is_file()
        assert (deployment[1] / "artifact/SHA256SUMS").is_file()


@pytest.mark.parametrize(
    ("script", "failure", "status", "message"),
    [
        ("ci-verify.sh", "FAIL_NETWORK", 71, "network-unavailable"),
        ("build-artifact.sh", "FAIL_BUILD", 72, "build-failed"),
    ],
)
def test_ci_scripts_preserve_real_docker_failures(
    deployment, script: str, failure: str, status: int, message: str
) -> None:
    # 不忽略网络创建或镜像构建错误,防止不完整制品进入部署
    result = run_ci_script(deployment, script, **{failure: "1"})
    assert result.returncode == status
    assert message in result.stderr
    assert not (deployment[1] / "artifact").exists()
