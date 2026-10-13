"""Local test/build/upload/release orchestration for the four Juya services."""

import argparse
import gzip
import json
import os
import re
import secrets
import shlex
import shutil
import sys
import tarfile
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (
    PROJECTS,
    load_config,
    remote_path,
    run_command,
    sanitized_environment,
    sha256_file,
)

HERE = Path(__file__).resolve().parent
REPOSITORIES = {project: project for project in PROJECTS}
REPOSITORIES["juya-miniapp-h5"] = "juya-miniapp"


def resolve_projects(selection):
    """Resolve a single project or dependency-ordered batch without duplicates."""
    if selection == "all":
        return list(PROJECTS)
    if selection not in PROJECTS:
        raise ValueError("unknown project: " + str(selection))
    return [selection]


def project_recipe(workspace, project):
    """Load a versioned repository recipe rather than caller-provided shell text."""
    repository = Path(workspace) / REPOSITORIES[project]
    path = repository / "deploy/local-release.json"
    recipe = json.loads(path.read_text(encoding="utf-8-sig"))
    if recipe.get("schema_version") != 1 or recipe.get("project") != project:
        raise ValueError("invalid project release recipe: " + project)
    if recipe.get("kind") not in {"web", "api"}:
        raise ValueError("invalid recipe kind")
    return repository, recipe


def python_for(repository):
    """Locate the project's own Python interpreter for isolated backend tests."""
    relative = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    return str(Path(repository) / relative)


def run_checks(recipe, repo):
    """Run all required quality gates; failure propagates before any upload."""
    env = sanitized_environment()
    for command in recipe.get("checks", []):
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) for arg in command)
        ):
            raise ValueError("recipe checks must be nonempty argv arrays")
        args = [arg.replace("{python}", python_for(repo)) for arg in command]
        print("[check] " + recipe["project"] + " " + " ".join(command), flush=True)
        run_command(args, cwd=repo, env=env)


def source_version(repository, environment):
    """Require a committed source version and explicit release branch."""
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=repository, capture=True)
    branch = run_command(["git", "branch", "--show-current"], cwd=repository, capture=True)
    dirty = run_command(["git", "status", "--porcelain"], cwd=repository, capture=True)
    if dirty:
        raise ValueError(
            str(repository.name) + " has uncommitted changes; commit the release first"
        )
    expected = "test" if environment == "test" else "main"
    if branch != expected:
        raise ValueError(
            str(repository.name) + " requires branch " + expected + " for " + environment
        )
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("invalid git commit")
    return commit, branch


def write_package(staging, archive):
    """Write a complete checksum list and a package containing only staged files."""
    staging, archive = Path(staging), Path(archive)
    files = sorted(path for path in staging.rglob("*") if path.is_file())
    sums = "".join(
        sha256_file(path) + "  " + path.relative_to(staging).as_posix() + "\n" for path in files
    )
    (staging / "SHA256SUMS").write_text(sums, encoding="utf-8")
    with tarfile.open(archive, "w:gz", compresslevel=1) as stream:
        for path in sorted(staging.rglob("*")):
            if path.is_symlink():
                raise ValueError("release staging must not contain symlinks")
            if path.is_file():
                stream.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
    return sha256_file(archive)


def build_bundle(project, environment, profile, workspace, output_root):
    """Build an environment-specific static site or Linux image after quality gates."""
    repository, recipe = project_recipe(workspace, project)
    commit, branch = source_version(repository, environment)
    release_id = (
        time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        + "-"
        + commit[:12]
        + "-"
        + secrets.token_hex(3)
    )
    output = Path(output_root) / project / release_id
    staging = output / "bundle"
    staging.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "project": project,
        "environment": environment,
        "kind": recipe["kind"],
        "commit": commit,
        "branch": branch,
        "release_id": release_id,
    }
    if recipe["kind"] == "web":
        build = recipe["build"][environment]
        env = dict(build.get("env", {}))
        env.update(
            VITE_API_BASE_URL=profile["expected_api_base"],
            VITE_USE_MOCK_API="false",
            VITE_LOCAL_DEV_MODE="false",
            VITE_ANALYTICS_ENABLED="false",
        )
        run_command(build["command"], cwd=repository, env=sanitized_environment(env))
        site = repository / recipe["site_output"]
        if site.is_symlink() or any(path.is_symlink() for path in site.rglob("*")):
            raise ValueError("frontend output must not contain symlinks")
        if not (site / "index.html").is_file() or not (site / "assets").is_dir():
            raise ValueError("frontend build did not produce an ECS website")
        if not any(
            profile["expected_api_base"] in path.read_text(encoding="utf-8", errors="ignore")
            for path in (site / "assets").rglob("*.js")
        ):
            raise ValueError("frontend bundle is missing its selected API base")
        shutil.copytree(site, staging / "site", symlinks=True)
    else:
        image = project + "-" + environment + ":" + commit
        args = [
            "docker",
            "build",
            "--progress=plain",
            "--platform",
            "linux/amd64",
            "--build-arg",
            "JUYA_SOURCE_COMMIT=" + commit,
            "--tag",
            image,
            "--file",
            recipe.get("dockerfile", "Dockerfile"),
            ".",
        ]
        run_command(args, cwd=repository, env=sanitized_environment())
        image_tar = output / "image.tar"
        run_command(["docker", "save", "--output", str(image_tar), image], cwd=repository)
        identity = run_command(
            [python_for(repository), "deploy/image-identity.py", str(image_tar), image],
            cwd=repository,
            capture=True,
        )
        with (
            image_tar.open("rb") as source,
            gzip.open(staging / "image.tar.gz", "wb", compresslevel=3) as target,
        ):
            shutil.copyfileobj(source, target, length=1024 * 1024)
        image_tar.unlink()
        (staging / "image-ref.txt").write_text(image + "\n", encoding="utf-8")
        (staging / "image-id.txt").write_text(identity + "\n", encoding="utf-8")
        manifest.update(image_ref=image, image_id=identity)
        deploy = staging / "deploy"
        deploy.mkdir()
        files = set(recipe["compose_files"][environment]) | {"image-identity.py"}
        files.update(recipe.get("deploy_files", []))
        for filename in ("redis-small.conf", "runtime-preflight.py"):
            if (repository / "deploy" / filename).is_file():
                files.add(filename)
        for filename in files:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename) or filename.startswith(".env"):
                raise ValueError("invalid deploy support filename")
            shutil.copy2(repository / "deploy" / filename, deploy / filename)
        profile["compose_files"] = list(recipe["compose_files"][environment])
        for key in (
            "services",
            "api_service",
            "worker_services",
            "beat_services",
            "migration_service",
        ):
            if key in recipe:
                profile[key] = recipe[key]
    # Ensure tracked sources did not change while checks/build were executing
    if source_version(repository, environment) != (commit, branch):
        raise ValueError("source changed during release build")
    (staging / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = output / (project + ".tgz")
    digest = write_package(staging, archive)
    return {"archive": archive, "sha256": digest, "manifest": manifest, "profile": profile}


def ssh_options(config, scp=False):
    """Construct validated SSH options with strict host-key and noninteractive auth."""
    ssh = config["ssh"]
    args = [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-P" if scp else "-p",
        str(ssh.get("port", 22)),
    ]
    identity = ssh.get("identity_file")
    if identity:
        if not Path(identity).is_file():
            raise ValueError("configured SSH identity file does not exist")
        args.extend(["-i", str(identity)])
    known_hosts = ssh.get("known_hosts_file")
    if known_hosts:
        args.extend(["-o", "UserKnownHostsFile=" + str(known_hosts)])
    return args


def ssh_command(config, argv):
    """Quote each remote argv component; connection data never becomes shell code."""
    ssh = config["ssh"]
    target = ssh["user"] + "@" + ssh["host"]
    command = " ".join(shlex.quote(str(arg)) for arg in argv)
    return run_command(["ssh", *ssh_options(config), target, command])


def upload_and_deploy(bundle, profile, config, action):
    """Upload to a unique private staging directory, then run the server handler."""
    release_id = (
        bundle["manifest"]["release_id"]
        if bundle
        else time.strftime("%Y%m%dT%H%M%S") + "-rollback-" + secrets.token_hex(4)
    )
    remote = "/tmp/juya-local-release/" + profile["project"] + "-" + release_id
    remote_path(remote)
    ssh_command(config, ["mkdir", "-p", "-m", "700", remote])
    local = Path(config["run_root"]) / "transport" / (profile["project"] + "-" + release_id)
    local.mkdir(parents=True)
    (local / "profile.json").write_text(json.dumps(profile) + "\n", encoding="utf-8")
    files = [HERE / filename for filename in ("remote.py", "common.py", "web.py", "api.py")]
    files.append(local / "profile.json")
    if bundle:
        files.append(bundle["archive"])
    target = config["ssh"]["user"] + "@" + config["ssh"]["host"] + ":" + remote + "/"
    run_command(["scp", *ssh_options(config, scp=True), *map(str, files), target])
    args = [
        "python3",
        remote + "/remote.py",
        "--profile",
        remote + "/profile.json",
        "--action",
        action,
        "--project",
        profile["project"],
        "--environment",
        profile["environment"],
    ]
    if bundle:
        args.extend(
            ["--bundle", remote + "/" + bundle["archive"].name, "--sha256", bundle["sha256"]]
        )
    ssh_command(config, args)


def doctor(config, projects, workspace):
    """Perform local and read-only ECS prerequisites without starting deployment."""
    run_command(
        [
            "ssh",
            *ssh_options(config),
            config["ssh"]["user"] + "@" + config["ssh"]["host"],
            "command -v python3 >/dev/null && command -v docker >/dev/null"
            " && command -v nginx >/dev/null",
        ]
    )
    for project in projects:
        profile = config["projects"].get(project)
        if not profile:
            raise ValueError("project is not configured in requested environment: " + project)
        _, recipe = project_recipe(workspace, project)
        if recipe["kind"] == "api":
            run_command(
                ["docker", "info", "--format", "{{.OSType}} {{.Architecture}}"], capture=True
            )
        if profile["kind"] == "api":
            ssh_command(config, ["test", "-r", profile["compose_env_file"]])
            ssh_command(config, ["test", "-d", profile["root"] + "/current-release/deploy"])
        else:
            ssh_command(config, ["test", "-e", profile["site_path"]])
        print("[doctor] " + project + " prerequisites available", flush=True)


def public_health(profile):
    """Verify the public HTTPS endpoint after the server's own readiness checks."""
    url = profile.get("public_health_url") or profile.get("api_url") or profile.get("site_url")
    if not url:
        return
    with urlopen(url, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("public health endpoint did not return 200")
        if "health/ready" in url and json.loads(response.read()).get("status") != "ready":
            raise RuntimeError("public API readiness did not report ready")


def record_stage(config, stage, project=None, manifest=None, elapsed_seconds=None):
    """Keep a recovery log containing release identity, stages and elapsed time."""
    root = Path(config["run_root"])
    root.mkdir(parents=True, exist_ok=True)
    event = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": stage}
    if project:
        event["project"] = project
    for field in ("release_id", "commit", "environment"):
        if manifest and field in manifest:
            event[field] = manifest[field]
    if elapsed_seconds is not None:
        event["elapsed_seconds"] = round(elapsed_seconds, 3)
    with (root / "release.log").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def format_elapsed(seconds):
    """Show a monotonic duration as hours/minutes/seconds, not a wall-clock date."""
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def terminal_supports_color():
    """Enable Windows console ANSI colors; keep redirected output plain text."""
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return os.environ.get("TERM") != "dumb"
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(-11)
        mode = wintypes.DWORD()
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 0x0004)
        )
    except (OSError, AttributeError):
        return False


class ReleaseProgress:
    """Count completed stages and report success only after all health checks."""

    def __init__(self, config, projects, environment, action, build_only):
        self.config = config
        self.count = len(projects)
        self.environment = "测试环境" if environment == "test" else "生产环境"
        self.action = "build" if build_only else action
        self.label = {
            "publish": "部署",
            "rollback": "回滚",
            "build": "本地检查和构建",
            "doctor": "预检查",
        }[self.action]
        self.steps = (
            2 * self.count
            if build_only
            else 1 + (0 if action == "doctor" else (2 if action == "rollback" else 4) * self.count)
        )
        self.done = 0
        self.started_at = time.monotonic()
        self.color = terminal_supports_color()

    def paint(self, text, color):
        return f"\033[1;{color}m{text}\033[0m" if self.color else text

    def show(self, message):
        percent = self.done * 100 // self.steps
        filled = self.done * 28 // self.steps
        bar = "#" * filled + "-" * (28 - filled)
        text = f"\n[总进度] [{bar}] {percent:3d}% ({self.done}/{self.steps})\n  {message}"
        print(self.paint(text, "36"), flush=True)

    def advance(self, message):
        self.done += 1
        if self.done < self.steps:
            self.show(message)

    def __enter__(self):
        self.show(self.label + "流程开始")
        return self

    def __exit__(self, error_type, error, traceback):
        elapsed = time.monotonic() - self.started_at
        state = (
            "completed"
            if error_type is None
            else ("interrupted" if issubclass(error_type, KeyboardInterrupt) else "failed")
        )
        try:
            record_stage(self.config, self.action + "_" + state, elapsed_seconds=elapsed)
        except OSError:
            if error_type is None:
                raise
        if error_type is None:
            self.show("全部步骤完成")
        status = (
            self.label + {"completed": "完成", "interrupted": "已中断", "failed": "失败"}[state]
        )
        if self.action in ("build", "doctor"):
            status += "(未部署)"
        separator = "=" * 64
        banner = (
            f"\n{separator}\n  {status} | {self.environment} | {self.count} 个项目\n"
            f"  总耗时: {format_elapsed(elapsed)}\n{separator}\n"
        )
        print(self.paint(banner, "32" if error_type is None else "31"), flush=True)
        return False


def run_release(config, projects, environment, action, workspace, build_only=False):
    """Complete every selected quality gate/build before any deployment begins."""
    if environment not in {"test", "production"} or action not in {
        "plan",
        "doctor",
        "publish",
        "rollback",
    }:
        raise ValueError("invalid release environment or action")
    if build_only and action != "publish":
        raise ValueError("build-only requires action publish")
    for project in projects:
        if project not in PROJECTS or project not in config.get("projects", {}):
            raise ValueError("release project is not configured")
    if action == "plan":
        for project in projects:
            _, recipe = project_recipe(workspace, project)
            print(
                "[plan] "
                + project
                + " "
                + environment
                + " "
                + recipe["kind"]
                + " test -> build -> upload -> deploy -> health/rollback"
            )
        return 0
    config = dict(config)
    config["run_root"] = (
        Path(workspace)
        / ".local-release/runs"
        / (time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3))
    )
    with ReleaseProgress(config, projects, environment, action, build_only) as progress:
        record_stage(config, action + "_started")
        if not build_only:
            progress.show("正在检查本地工具和 ECS 连接")
            doctor(config, projects, workspace)
            progress.advance("预检查通过")
        if action == "doctor":
            return 0
        if action == "rollback":
            for project in projects:
                started_at = time.monotonic()
                progress.show(project + ": 正在上传回滚工具并恢复上一版本")
                upload_and_deploy(None, config["projects"][project], config, "rollback")
                progress.advance(project + ": ECS 回滚步骤通过")
                progress.show(project + ": 正在验证公网健康检查")
                public_health(config["projects"][project])
                progress.advance(project + ": 健康检查通过")
                elapsed = time.monotonic() - started_at
                record_stage(config, "rolled_back", project, elapsed_seconds=elapsed)
                print(
                    progress.paint(
                        "[rolled_back] "
                        + project
                        + " | 回滚及健康检查耗时 "
                        + format_elapsed(elapsed),
                        "32",
                    ),
                    flush=True,
                )
            return 0
        versions = {}
        for project in projects:
            repository, _ = project_recipe(workspace, project)
            versions[project] = source_version(repository, environment)
        for project in projects:
            repository, recipe = project_recipe(workspace, project)
            progress.show(project + ": 正在检查和测试")
            run_checks(recipe, repository)
            record_stage(config, "checks_passed", project)
            progress.advance(project + ": 检查和测试通过")
        bundles = []
        for project in projects:
            repository, _ = project_recipe(workspace, project)
            if source_version(repository, environment) != versions[project]:
                raise ValueError("source changed after checks")
            progress.show(project + ": 正在构建和打包")
            bundles.append(
                build_bundle(
                    project,
                    environment,
                    dict(config["projects"][project]),
                    workspace,
                    config["run_root"],
                )
            )
            record_stage(config, "built", project, bundles[-1]["manifest"])
            progress.advance(project + ": 构建和打包完成")
        if build_only:
            for bundle in bundles:
                print("[built] " + str(bundle["archive"]), flush=True)
            return 0
        for bundle in bundles:
            profile = bundle["profile"]
            started_at = time.monotonic()
            progress.show(profile["project"] + ": 正在上传并部署到 ECS")
            upload_and_deploy(bundle, profile, config, "publish")
            progress.advance(profile["project"] + ": ECS 部署步骤通过")
            progress.show(profile["project"] + ": 正在验证公网健康检查")
            try:
                public_health(profile)
            except (Exception, KeyboardInterrupt):
                progress.show(profile["project"] + ": 公网检查失败, 正在恢复上一版本")
                print("[health] public check failed; restoring previous application", flush=True)
                upload_and_deploy(None, profile, config, "rollback")
                record_stage(config, "health_failed_rolled_back", profile["project"])
                raise
            progress.advance(profile["project"] + ": 健康检查通过")
            elapsed = time.monotonic() - started_at
            record_stage(
                config, "released", profile["project"], bundle["manifest"], elapsed_seconds=elapsed
            )
            print(
                progress.paint(
                    "[released] "
                    + profile["project"]
                    + " "
                    + environment
                    + " "
                    + bundle["manifest"]["release_id"]
                    + " | 上传、部署及健康检查耗时 "
                    + format_elapsed(elapsed),
                    "32",
                ),
                flush=True,
            )
        return 0


def main(argv=None):
    """Parse CLI/menu selections and return a nonzero result on any failed stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=["all", *PROJECTS])
    parser.add_argument("--environment", choices=["test", "production"])
    parser.add_argument(
        "--action", choices=["plan", "doctor", "publish", "rollback"], default="publish"
    )
    parser.add_argument("--workspace", type=Path, default=HERE.parents[2])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.project or not args.environment:
        if not sys.stdin.isatty():
            parser.error("--project and --environment are required outside an interactive terminal")
        choices = ["all", *PROJECTS]
        print("Projects: " + ", ".join(str(i + 1) + "=" + name for i, name in enumerate(choices)))
        if not args.project:
            choice = input("Select project [1]: ").strip() or "1"
            if not choice.isdigit() or not 1 <= int(choice) <= len(choices):
                parser.error("invalid project selection")
            args.project = choices[int(choice) - 1]
        if not args.environment:
            choice = input("Environment [1=test, 2=production; default 1]: ").strip() or "1"
            if choice not in {"1", "2"}:
                parser.error("invalid environment selection")
            args.environment = "test" if choice == "1" else "production"
    config_path = args.config or args.workspace / ".local-release/config.json"
    try:
        config = load_config(config_path, args.environment, args.workspace)
        return run_release(
            config,
            resolve_projects(args.project),
            args.environment,
            args.action,
            args.workspace,
            args.build_only,
        )
    except KeyboardInterrupt:
        print("Release interrupted; server handlers retain application rollback", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        # Error messages contain stage names only; never echo configuration or command output
        print("Release stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
