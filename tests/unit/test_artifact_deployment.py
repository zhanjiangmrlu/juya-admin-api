import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SHA = "a" * 40
IMAGE = f"juya-admin-api-test:{SHA}"
CONFIG = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
IMAGE_ID = "sha256:" + hashlib.sha256(CONFIG).hexdigest()
OCI_MANIFEST = json.dumps(
    {"schemaVersion": 2, "config": {"digest": IMAGE_ID}, "layers": []}
).encode()
MANIFEST_ID = "sha256:" + hashlib.sha256(OCI_MANIFEST).hexdigest()
OCI_INDEX = json.dumps({"schemaVersion": 2, "manifests": [{"digest": MANIFEST_ID}]}).encode()
INDEX_ID = "sha256:" + hashlib.sha256(OCI_INDEX).hexdigest()


def write_image_archive(
    path: Path, *, tag: str = IMAGE, config: bytes = CONFIG, indexed: bool = False
) -> None:
    # 使用真实 Docker/OCI 元数据模拟镜像包,不调用真实 Docker 或数据库。
    entries = {
        "manifest.json": json.dumps(
            [{"Config": "blobs/sha256/" + IMAGE_ID[7:], "RepoTags": [tag], "Layers": []}]
        ).encode(),
        "blobs/sha256/" + IMAGE_ID[7:]: config,
        "blobs/sha256/" + MANIFEST_ID[7:]: OCI_MANIFEST,
        "index.json": json.dumps(
            {"schemaVersion": 2, "manifests": [{"digest": INDEX_ID if indexed else MANIFEST_ID}]}
        ).encode(),
    }
    if indexed:
        entries["blobs/sha256/" + INDEX_ID[7:]] = OCI_INDEX
    with tarfile.open(path, "w:gz" if path.suffix == ".gz" else "w") as archive:
        for name, data in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))


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
    shutil.copyfile(ROOT / "deploy/image-identity.py", bundle / "deploy/image-identity.py")
    write_image_archive(bundle / "image.tar.gz")
    write_image_archive(tmp_path / "fixture-image.tar")
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
        '  "build "*)\n'
        '    [ "${FAIL_BUILD:-0}" != 1 ] || { echo build-failed >&2; exit 72; };;\n'
        '  "save --output "*) cp "$FIXTURE_ARCHIVE" "$3";;\n'
        f'  *"image inspect"*) echo "${{LOADED_IMAGE_ID:-{IMAGE_ID}}}";;\n'
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
    (binaries / "python3").write_text(
        f"#!/bin/sh\nexec '{posix_path(Path(sys.executable))}' \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
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
        "FIXTURE_ARCHIVE": posix_path(tmp_path / "fixture-image.tar"),
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


def test_accepts_containerd_manifest_id_for_same_image(deployment, tmp_path: Path) -> None:
    # Docker 29 的 manifest ID 与配置摘要不同,同一镜像必须正常发布。
    result = run_deploy(deployment, LOADED_IMAGE_ID=MANIFEST_ID)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "server/current-release").is_symlink()


def test_rejects_other_loaded_image_before_backup(deployment, tmp_path: Path) -> None:
    result = run_deploy(deployment, LOADED_IMAGE_ID="sha256:" + "c" * 64)
    assert result.returncode == 65
    assert "image identity mismatch" in result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "mysqldump" not in calls and "stop " not in calls


@pytest.mark.parametrize("tag,config", [("other:tag", CONFIG), (IMAGE, CONFIG + b" ")])
def test_identity_rejects_wrong_tag_or_changed_config(tmp_path: Path, tag: str, config: bytes):
    archive = tmp_path / "image.tar.gz"
    write_image_archive(archive, tag=tag, config=config)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/image-identity.py"),
            str(archive),
            IMAGE,
            IMAGE_ID,
            MANIFEST_ID,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 65


def test_identity_accepts_index_containing_same_platform(tmp_path: Path):
    archive = tmp_path / "image.tar.gz"
    write_image_archive(archive, indexed=True)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy/image-identity.py"),
            str(archive),
            IMAGE,
            IMAGE_ID,
            INDEX_ID,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
    arguments = {
        "build-artifact.sh": ["test", SHA, "artifact"],
        "ci-install-rpm.sh": [
            "gcc",
            "make",
            "tar",
            "gzip",
            "xz",
            "libaio",
            "numactl-libs",
            "ncurses-libs",
            "libtirpc",
        ],
    }.get(script, [])
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


def test_build_continues_when_version_api_is_unsupported(deployment, tmp_path: Path) -> None:
    # 云效版本查询失败不能阻止镜像构建;其他 Docker 操作由隔离夹具模拟
    result = run_ci_script(deployment, "build-artifact.sh")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "build --progress=plain --platform linux/amd64" in calls
    assert (deployment[1] / "artifact/image.tar.gz").is_file()
    assert (deployment[1] / "artifact/SHA256SUMS").is_file()


def test_build_records_portable_config_digest_and_timings(deployment):
    result = run_ci_script(deployment, "build-artifact.sh", LOADED_IMAGE_ID=MANIFEST_ID)
    assert result.returncode == 0, result.stderr
    assert (deployment[1] / "artifact/image-id.txt").read_text().strip() == IMAGE_ID
    assert "docker-build" in result.stdout and "elapsed=" in result.stdout


@pytest.mark.parametrize(
    ("script", "failure", "status", "message"),
    [
        ("build-artifact.sh", "FAIL_BUILD", 72, "build-failed"),
    ],
)
def test_ci_scripts_preserve_real_docker_failures(
    deployment, script: str, failure: str, status: int, message: str
) -> None:
    # 不忽略镜像构建错误,防止不完整制品进入部署
    result = run_ci_script(deployment, script, **{failure: "1"})
    assert result.returncode == status
    assert message in result.stderr
    assert not (deployment[1] / "artifact").exists()


def native_tools(deployment, tmp_path: Path, *, fail_test: bool = False) -> None:
    # 模拟原生服务,只启动夹具自有 sleep 进程;不操作 Docker 或真实数据库
    binaries = tmp_path / "bin"
    scripts = {
        "uname": "#!/bin/sh\necho Linux\n",
        "mysqld": (
            '#!/bin/sh\ncase "$*" in\n'
            '  *--version*) echo "mysqld  Ver 8.4.6";;\n'
            "  *--initialize-insecure*) exit 0;;\n"
            "  *) exec sleep 60;;\nesac\n"
        ),
        "mysql": '#!/bin/sh\nprintf "mysql|%s\\n" "$*" >> "$CALL_LOG"\n',
        "redis-server": (
            '#!/bin/sh\ncase "$*" in\n'
            '  *--version*) echo "Redis server v=7.4.2";;\n'
            "  *) exec sleep 60;;\nesac\n"
        ),
        "redis-cli": "#!/bin/sh\necho PONG\n",
        # Shell orchestration fixtures; real decoding is covered by audio_pipeline tests.
        "ffmpeg": (
            '#!/bin/sh\ncase "$*" in\n'
            '  *-version*) echo "ffmpeg version 7.1.5";;\n'
            "  *) exit 0;;\nesac\n"
        ),
        "ffprobe": (
            '#!/bin/sh\ncase "$*" in\n'
            '  *-version*) echo "ffprobe version 7.1.5";;\n'
            '  *) echo "duration=0.100000";;\nesac\n'
        ),
        "uv": (
            "#!/bin/sh\n"
            'if [ "${EXPECT_CLEAN_ENV:-0}" = 1 ]; then\n'
            "  for variable in JUYA_DATABASE_URL JUYA_REDIS_URL JUYA_OSS_ACCESS_KEY_ID "
            "JUYA_OSS_ACCESS_KEY_SECRET JUYA_OSS_BUCKET OSS_ACCESS_KEY_ID "
            "OSS_ACCESS_KEY_SECRET OSS_SESSION_TOKEN; do\n"
            '    if env | cut -d= -f1 | grep -x "$variable" >/dev/null; then\n'
            "      echo inherited-business-configuration >&2; exit 74; fi\n"
            "  done\n"
            '  [ "${JUYA_RUN_LIVE_OSS_TESTS:-}" = false ] && '
            '[ "${JUYA_RUN_LIVE_OSS_BROWSER_TESTS:-}" = false ] || exit 74\n'
            'fi\nprintf "uv|%s\\n" "$*" >> "$CALL_LOG"\n'
            'case "$*" in "pip sync "*) [ "${FAIL_PYTHON_SYNC:-0}" = 0 ] || exit 77;; esac\n'
            + ('case "$*" in "run --no-sync pytest "*) exit 73;; esac\n' if fail_test else "")
        ),
        # 清理交给 pytest,避免在 Windows 夹具中调用 Shell 递归删除
        "rm": "#!/bin/sh\nexit 0\n",
    }
    for filename, contents in scripts.items():
        (binaries / filename).write_text(contents, encoding="utf-8", newline="\n")
    runtime = tmp_path / "native-runtime"
    runtime.mkdir()
    deployment[2]["TMPDIR"] = posix_path(runtime)
    deployment[2]["XDG_CACHE_HOME"] = posix_path(tmp_path / "cache")
    subprocess.run([deployment[0], "-c", f"chmod +x '{posix_path(binaries)}'/*"], check=True)


def test_native_ci_runs_checks_without_any_docker_calls(deployment, tmp_path: Path) -> None:
    native_tools(deployment, tmp_path)
    result = run_ci_script(deployment, "ci-verify.sh")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert all(line.startswith(("mysql|", "uv|")) for line in calls.splitlines())
    assert "uv|run --no-sync alembic upgrade head" in calls
    assert "uv|run --no-sync ruff check ." in calls
    assert "uv|run --no-sync mypy src" in calls
    assert "uv|run --no-sync pytest --ignore=" in calls
    assert "--cov-append" in calls
    assert "8.163.84.24" not in calls


def test_native_ci_installs_locked_hashes_from_mirror_without_resync(deployment, tmp_path):
    # 国内镜像只改变下载地址,锁文件导出的版本和哈希仍强制校验。
    native_tools(deployment, tmp_path)
    result = run_ci_script(deployment, "ci-verify.sh")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "export --locked --no-emit-project" in calls
    sync = next(line for line in calls.splitlines() if line.startswith("uv|pip sync "))
    assert "--require-hashes" in sync
    assert "--index-url https://mirrors.aliyun.com/pypi/simple/" in sync
    assert "uv|sync " not in calls
    assert all(
        "run --no-sync " in line for line in calls.splitlines() if line.startswith("uv|run ")
    )


def test_native_ci_stops_before_database_when_python_install_fails(deployment, tmp_path):
    # 下载失败不能被当作环境就绪,也不能提前操作数据库。
    native_tools(deployment, tmp_path)
    result = run_ci_script(deployment, "ci-verify.sh", FAIL_PYTHON_SYNC="1")
    assert result.returncode == 77
    calls = (tmp_path / "calls").read_text()
    assert "mysql|" not in calls and "uv|run --no-sync pytest " not in calls


def test_native_ci_stops_on_test_failure(deployment, tmp_path: Path) -> None:
    native_tools(deployment, tmp_path, fail_test=True)
    result = run_ci_script(deployment, "ci-verify.sh")
    assert result.returncode == 73
    calls = (tmp_path / "calls").read_text()
    assert "uv|run --no-sync pytest --ignore=" in calls
    assert "--cov-append" not in calls


def test_native_ci_clears_inherited_business_connections_and_live_flags(
    deployment, tmp_path: Path
) -> None:
    # 流水线业务变量不能污染原生检查或触发真实 OSS 操作;这里只注入虚构值
    native_tools(deployment, tmp_path)
    result = run_ci_script(
        deployment,
        "ci-verify.sh",
        EXPECT_CLEAN_ENV="1",
        JUYA_DATABASE_URL="mysql+pymysql://fixture:fixture@example.invalid/business",
        JUYA_REDIS_URL="redis://example.invalid/0",
        JUYA_OSS_ACCESS_KEY_ID="fixture-id",
        JUYA_OSS_ACCESS_KEY_SECRET="fixture-secret",
        JUYA_OSS_BUCKET="fixture-bucket",
        OSS_ACCESS_KEY_ID="fixture-id",
        OSS_ACCESS_KEY_SECRET="fixture-secret",
        OSS_SESSION_TOKEN="fixture-token",
        JUYA_RUN_LIVE_OSS_TESTS="true",
        JUYA_RUN_LIVE_OSS_BROWSER_TESTS="true",
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "--cov-append" in calls
    assert "example.invalid" not in calls


def test_native_ci_rejects_non_test_branch_before_starting_services(
    deployment, tmp_path: Path
) -> None:
    native_tools(deployment, tmp_path)
    deployment[2]["CI_COMMIT_REF_NAME"] = "main"
    result = run_ci_script(deployment, "ci-verify.sh", CI_COMMIT_REF_NAME="main")
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize("missing", ["", "libaio numactl-libs"])
def test_rpm_bootstrap_only_installs_missing_packages_from_base_repo(
    deployment, tmp_path: Path, missing: str
) -> None:
    # 功能:已有依赖不刷新软件源,缺少依赖时只请求基础仓库且保留签名验证
    # 参数 deployment 为命令夹具,tmp_path 为临时目录,missing 为未安装的依赖名
    binaries = tmp_path / "bin"
    (binaries / "rpm").write_text(
        '#!/bin/sh\ncase " ${MISSING_RPMS:-} " in *" $2 "*) exit 1;; esac\n',
        encoding="utf-8",
        newline="\n",
    )
    (binaries / "dnf").write_text(
        '#!/bin/sh\nprintf "dnf|%s\\n" "$*" >> "$CALL_LOG"\nexit "${FAIL_DNF:-0}"\n',
        encoding="utf-8",
        newline="\n",
    )
    subprocess.run([deployment[0], "-c", f"chmod +x '{posix_path(binaries)}'/*"], check=True)
    result = run_ci_script(deployment, "ci-install-rpm.sh", MISSING_RPMS=missing)
    assert result.returncode == 0, result.stderr
    if not missing:
        assert not (tmp_path / "calls").exists()
        return
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 1
    assert "--disablerepo=* --enablerepo=alinux3-os" in calls[0]
    assert "https://mirrors.aliyun.com/alinux/3/os/$basearch/" in calls[0]
    assert calls[0].endswith("libaio numactl-libs")
    assert "--nogpgcheck" not in calls[0] and "gpgcheck=0" not in calls[0]
    assert "--setopt=alinux3-os.gpgcheck=1" in calls[0]
    assert "updates" not in calls[0] and "plus" not in calls[0]


def test_rpm_bootstrap_preserves_install_failure(deployment, tmp_path: Path) -> None:
    # 功能:依赖安装失败必须阻止后续测试,不能将缺少库的环境当作可用
    # 参数 deployment 为命令夹具,tmp_path 为命令记录临时目录
    binaries = tmp_path / "bin"
    (binaries / "rpm").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (binaries / "dnf").write_text("#!/bin/sh\nexit 76\n", encoding="utf-8")
    subprocess.run([deployment[0], "-c", f"chmod +x '{posix_path(binaries)}'/*"], check=True)
    result = run_ci_script(deployment, "ci-install-rpm.sh")
    assert result.returncode == 76
