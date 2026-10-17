"""Shared release validation; no application dependencies or credentials in packages."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

PROJECTS = ("juya-admin-api", "juya-miniapp-api", "juya-admin", "juya-miniapp-h5")
ENVIRONMENTS = ("test", "production")


def sha256_file(path):
    """Hash a file without loading Docker image archives into memory."""
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def remote_path(value):
    """Validate a configured absolute server path before it reaches SSH."""
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise ValueError("server paths must be absolute and contain no shell characters")
    parts = PurePosixPath(value).parts
    if ".." in parts or len(parts) < 3 or value.endswith("/") or value.startswith("//"):
        raise ValueError("server path is too broad or traverses its parent")
    return PurePosixPath(value).as_posix()


def _https(value):
    """Require an explicit HTTPS business endpoint without embedded credentials."""
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("business endpoints require HTTPS without credentials")
    return value


def load_config(path, environment, workspace):
    """Load only the requested environment and reject overlapping production targets."""
    if environment not in ENVIRONMENTS:
        raise ValueError("environment must be test or production")
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != 1:
        raise ValueError("unsupported release configuration version")
    environments = value.get("environments", {})
    selected = environments.get(environment)
    if not isinstance(selected, dict):
        raise ValueError("requested environment is not configured")
    selected = json.loads(json.dumps(selected))
    ssh = selected.get("ssh", {})
    host, user = ssh.get("host", ""), ssh.get("user", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("requested environment needs its own SSH host")
    host = host.lower().rstrip(".")
    ssh["host"] = host
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user):
        raise ValueError("SSH user is missing or invalid")
    port = ssh.get("port", 22)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    ssh["port"] = port
    projects = selected.get("projects", {})
    if not isinstance(projects, dict) or not projects:
        raise ValueError("requested environment has no project targets")
    for project, profile in projects.items():
        if project not in PROJECTS or not isinstance(profile, dict):
            raise ValueError("unknown release project")
        if profile.get("environment", environment) != environment:
            raise ValueError("profile environment does not match selection")
        profile.update(project=project, environment=environment)
        profile["root"] = remote_path(profile.get("root"))
        kind = profile.get("kind")
        if kind == "web":
            profile["site_path"] = remote_path(profile.get("site_path"))
            for field in ("site_url", "api_url", "expected_api_base"):
                _https(profile.get(field))
        elif kind == "api":
            profile["compose_env_file"] = remote_path(profile.get("compose_env_file"))
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile.get("compose_project", "")):
                raise ValueError("Compose project is missing or invalid")
        else:
            raise ValueError("release kind must be web or api")
        for extra in profile.get("compose_extra_files", []):
            remote_path(extra)
    if environment == "production":
        test = environments.get("test", {})
        if str(test.get("ssh", {}).get("host", "")).lower().rstrip(".") == host:
            for project, profile in projects.items():
                for previous in test.get("projects", {}).values():
                    if profile.get("compose_project") and profile.get(
                        "compose_project"
                    ) == previous.get("compose_project"):
                        raise ValueError("production Compose project overlaps test")
                    fields = ("root", "site_path", "compose_env_file")
                    for field in fields:
                        if not profile.get(field):
                            continue
                        target = PurePosixPath(profile[field])
                        for previous_field in fields:
                            if not previous.get(previous_field):
                                continue
                            old = PurePosixPath(previous[previous_field])
                            if target.is_relative_to(old) or old.is_relative_to(target):
                                raise ValueError(
                                    "production target overlaps test: " + project + "/" + field
                                )
    selected["environment"] = environment
    return selected


def sanitized_environment(overrides=None):
    """Isolate local tests/builds from inherited business and frontend overrides."""
    result = {
        key: val
        for key, val in os.environ.items()
        if not key.startswith(("JUYA_", "OSS_", "VITE_", "UNI_"))
    }
    result.update(
        PYTHONIOENCODING="utf-8",
        JUYA_RUN_LIVE_OSS_TESTS="false",
        JUYA_RUN_LIVE_OSS_BROWSER_TESTS="false",
    )
    result.update(overrides or {})
    return result


def executable_command(args):
    """Run Windows pnpm through its Node entrypoint without shell interpolation."""
    args = [str(item) for item in args]
    if os.name == "nt" and args[0] in {"pnpm", "pnpm.cmd"}:
        shim = shutil.which("pnpm") or shutil.which("pnpm.cmd")
        if not shim:
            raise ValueError("pnpm is not installed")
        directory = Path(shim).parent
        candidates = (
            directory / "node_modules/corepack/dist/pnpm.js",
            directory / "node_modules/pnpm/bin/pnpm.cjs",
        )
        entry = next((candidate for candidate in candidates if candidate.is_file()), None)
        if entry is None:
            raise ValueError("pnpm Node entrypoint was not found; install pnpm through Corepack")
        return [shutil.which("node") or "node", str(entry), *args[1:]]
    return args


def run_command(args, cwd=None, env=None, capture=False):
    """Execute argv directly; never include credentials in logged command text."""
    result = subprocess.run(
        executable_command(args),
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if result.returncode:
        raise RuntimeError("command failed (exit " + str(result.returncode) + "): " + str(args[0]))
    return result.stdout.strip() if capture else ""


def _member_path(name):
    """Reject absolute, Windows, parent-traversing, and control-character tar paths."""
    if not isinstance(name, str) or "\\" in name or ":" in name or any(ord(c) < 32 for c in name):
        raise ValueError("unsafe archive path")
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError("unsafe archive path")
    return value.as_posix()


def validate_bundle(archive, expected_sha256, project, environment, destination):
    """Validate transport, every file, and identity before publishing extracted files."""
    archive, destination = Path(archive), Path(destination)
    if project not in PROJECTS or environment not in ENVIRONMENTS:
        raise ValueError("invalid deployment selection")
    if (
        not re.fullmatch(r"[a-f0-9]{64}", str(expected_sha256))
        or sha256_file(archive) != expected_sha256
    ):
        raise ValueError("release transport checksum mismatch")
    if destination.exists() or destination.is_symlink():
        raise ValueError("release extraction destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="juya-validated-", dir=destination.parent) as owned:
        staging = Path(owned)
        files = set()
        paths = set()
        total_size = 0
        with tarfile.open(archive, "r:*") as stream:
            for member in stream:
                name = _member_path(member.name)
                if name in paths or not (member.isfile() or member.isdir()):
                    raise ValueError("duplicate or non-regular release member")
                paths.add(name)
                total_size += member.size
                if len(paths) > 30000 or total_size > 16 * 1024**3:
                    raise ValueError("release archive exceeds extraction budget")
                target = staging.joinpath(*PurePosixPath(name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                files.add(name)
        if not {"release.json", "SHA256SUMS"}.issubset(files):
            raise ValueError("release manifest or checksum list is missing")
        manifest_path, sums_path = staging / "release.json", staging / "SHA256SUMS"
        if manifest_path.stat().st_size > 1024 * 1024 or sums_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("release metadata exceeds size budget")
        listed = set()
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([a-f0-9]{64}) [ *](.+)", line)
            if not match:
                raise ValueError("invalid release checksum entry")
            digest, name = match.groups()
            name = _member_path(name)
            if name in listed or name == "SHA256SUMS" or name not in files:
                raise ValueError("duplicate or missing checksum file")
            if sha256_file(staging / name) != digest:
                raise ValueError("release file checksum mismatch: " + name)
            listed.add(name)
        if listed != files - {"SHA256SUMS"}:
            raise ValueError("release contains files missing from its checksum list")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version"),
            manifest.get("project"),
            manifest.get("environment"),
        ) != (1, project, environment):
            raise ValueError("release identity does not match deployment target")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,100}", manifest.get("release_id", "")):
            raise ValueError("invalid release identifier")
        if not re.fullmatch(r"[a-f0-9]{40}", manifest.get("commit", "")):
            raise ValueError("invalid source commit")
        if manifest.get("kind") not in {"web", "api"}:
            raise ValueError("invalid release kind")
        # staging is owned by this invocation; only the validated directory is moved
        staging.rename(destination)
    return manifest
