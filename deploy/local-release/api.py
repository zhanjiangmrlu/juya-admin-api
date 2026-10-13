"""Transactional ECS application releases (Python 3.12 standard library only)."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4


class ReleaseError(RuntimeError):
    """A sanitized deployment failure; command output can contain server secrets."""


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseError("release metadata is missing or invalid") from error


def _safe_relative(value):
    if not isinstance(value, str) or "\\" in value or "\n" in value:
        raise ReleaseError("invalid release relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ReleaseError("invalid release relative path")
    return path


def _verify_files(directory, *, legacy=False):
    """Validate retained releases again before trusting them for recovery."""
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise ReleaseError("release directory must be a real directory")
        inventory = {}
        for line in (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([a-f0-9]{64}) [ *](.+)", line)
            if not match:
                raise ReleaseError("invalid release checksum inventory")
            name = str(_safe_relative(match[2]))
            if name in inventory or name == "SHA256SUMS":
                raise ReleaseError("duplicate release checksum entry")
            inventory[name] = match[1]
        actual = set()
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ReleaseError("release contains a symlink")
            if not path.is_file():
                if not path.is_dir():
                    raise ReleaseError("release contains a special file")
                continue
            name = path.relative_to(directory).as_posix()
            if name == "SHA256SUMS":
                continue
            actual.add(name)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if inventory.get(name) != digest.hexdigest():
                raise ReleaseError("release checksum mismatch")
        required = {"image-ref.txt", "image-id.txt"}
        if not legacy:
            required |= {"release.json", "image.tar.gz"}
        complete = actual == set(inventory) if not legacy else actual <= set(inventory)
        if not complete or not required <= actual:
            raise ReleaseError("release checksum inventory is incomplete")
    except OSError as error:
        raise ReleaseError("release checksum verification failed") from error


def _manifest(directory, profile, supplied=None):
    if not (directory / "release.json").is_file():
        raise ReleaseError("legacy release lacks complete image metadata; conversion is required")
    _verify_files(directory)
    manifest = _read_json(directory / "release.json")
    if not isinstance(manifest, dict) or (supplied is not None and supplied != manifest):
        raise ReleaseError("release manifest mismatch")
    if (manifest.get("schema_version"), manifest.get("kind")) != (1, "api"):
        raise ReleaseError("unsupported API release manifest")
    for field in ("project", "environment"):
        if manifest.get(field) != profile[field]:
            raise ReleaseError("release " + field + " mismatch")
    if not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("commit", ""))):
        raise ReleaseError("invalid release commit")
    if not isinstance(manifest.get("branch"), str) or not manifest["branch"]:
        raise ReleaseError("missing release branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", str(manifest.get("release_id", ""))):
        raise ReleaseError("invalid release ID")
    image = manifest.get("image_ref", "")
    prefix = profile["project"] + "-" + profile["environment"] + ":"
    if (
        not isinstance(image, str)
        or not image.startswith(prefix)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image)
    ):
        raise ReleaseError("image reference does not belong to project/environment")
    image_id = manifest.get("image_id", "")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ReleaseError("invalid image identity")
    if manifest.get("legacy_snapshot") and not re.fullmatch(
        r"sha256:[a-f0-9]{64}", str(manifest.get("rollback_image_id", ""))
    ):
        raise ReleaseError("legacy snapshot lacks immutable rollback identity")
    try:
        if (directory / "image-ref.txt").read_text().strip() != image or (
            directory / "image-id.txt"
        ).read_text().strip() != image_id:
            raise ReleaseError("image metadata mismatch")
    except OSError as error:
        raise ReleaseError("missing image metadata") from error
    return manifest


def _image_identity(archive_path, image, expected, *, untagged=False):
    """Accept config and matching OCI digests, never merely an image tag."""
    metadata = {}
    total = 0
    try:
        with tarfile.open(archive_path, "r|*") as archive:
            seen = set()
            for member in archive:
                name = str(_safe_relative(member.name.rstrip("/")))
                if name in seen or not (member.isfile() or member.isdir()):
                    raise ReleaseError("unsafe image archive entry")
                seen.add(name)
                if not member.isfile() or member.size > 1024 * 1024:
                    continue
                if name.endswith(".json") or name.startswith("blobs/sha256/"):
                    total += member.size
                    if total > 32 * 1024 * 1024:
                        raise ReleaseError("image metadata exceeds safe limit")
                    metadata[name] = archive.extractfile(member).read()
        records = json.loads(metadata["manifest.json"])
        tags = records[0].get("RepoTags") if len(records) == 1 else []
        if len(records) != 1 or (tags != [image] and not (untagged and tags in (None, []))):
            raise ReleaseError("image archive tag mismatch")
        config_path = records[0]["Config"]
        raw = metadata[config_path]
        config_id = "sha256:" + hashlib.sha256(raw).hexdigest()
        if config_path not in (config_id[7:] + ".json", "blobs/sha256/" + config_id[7:]):
            raise ReleaseError("image archive config digest mismatch")
        config = json.loads(raw)
        if (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
            raise ReleaseError("image must target Linux/amd64")
        if config_id != expected:
            raise ReleaseError("image identity does not match manifest")
        accepted = {config_id}

        def visit(descriptor, ancestors):
            digest = descriptor.get("digest", "")
            if (
                not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                or digest in ancestors
                or len(ancestors) > 8
            ):
                raise ReleaseError("invalid image OCI graph")
            content = metadata["blobs/sha256/" + digest[7:]]
            if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
                raise ReleaseError("image OCI digest mismatch")
            record = json.loads(content)
            matched = record.get("config", {}).get("digest") == config_id
            for child in record.get("manifests", []):
                if visit(child, ancestors | {digest}):
                    matched = True
            if matched:
                accepted.add(digest)
            return matched

        for descriptor in json.loads(metadata.get("index.json", b'{"manifests":[]}')).get(
            "manifests", []
        ):
            visit(descriptor, set())
        return accepted
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError) as error:
        raise ReleaseError("image archive identity verification failed") from error


def _env_file(path, keys):
    """Read only locally on the server; values never enter logs or exceptions."""
    result = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, separator, value = line.partition("=")
            if key.strip() not in keys:
                continue
            if not separator:
                raise ReleaseError("invalid server environment file")
            value = value.strip()
            if value[:1] in ("'", '"'):
                quote = value[0]
                end = value.find(quote, 1)
                trailing = value[end + 1 :].strip()
                if end < 0 or (trailing and not trailing.startswith("#")):
                    raise ReleaseError("unsupported server environment value")
                value = value[1:end]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            result[key.strip()] = value
    except OSError as error:
        raise ReleaseError("server environment file is unavailable") from error
    return result


def _profile(profile):
    if profile.get("project") not in ("juya-admin-api", "juya-miniapp-api") or profile.get(
        "environment"
    ) not in ("test", "production"):
        raise ReleaseError("invalid API project/environment")
    root = Path(profile.get("root", ""))
    env_file = Path(profile.get("compose_env_file", ""))
    if not root.is_absolute() or root == Path(root.anchor) or root.is_symlink():
        raise ReleaseError("invalid API release root")
    if not env_file.is_absolute() or not env_file.is_file():
        raise ReleaseError("explicit server Compose environment file is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(profile.get("compose_project", ""))):
        raise ReleaseError("invalid Compose project")
    services = profile.get("services")
    workers, beats = profile.get("worker_services"), profile.get("beat_services")
    if not all(isinstance(value, list) for value in (services, workers, beats)):
        raise ReleaseError("explicit application service lists are required")
    roles = [profile.get("api_service"), *workers, *beats]
    if (
        len(set(roles)) != len(roles)
        or set(roles) != set(services)
        or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) for name in roles
        )
        or any(name in ("mysql", "redis", "migrate") for name in roles)
    ):
        raise ReleaseError("invalid application service selection")
    migration = profile.get("migration_service")
    if profile["project"] == "juya-admin-api":
        if not isinstance(migration, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", migration):
            raise ReleaseError("admin migration service is required")
    elif migration:
        raise ReleaseError("miniapp cannot own database migrations")
    files = profile.get("compose_files")
    if not isinstance(files, list) or not files:
        raise ReleaseError("explicit Compose files are required")
    for value in files:
        _safe_relative(value)
    extra = profile.get("compose_extra_files", [])
    if not isinstance(extra, list) or any(
        not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file()
        for value in extra
    ):
        raise ReleaseError("server Compose override is unavailable")
    environment = _env_file(env_file, {"JUYA_RUNTIME_ENV_FILE"})
    runtime = Path(environment.get("JUYA_RUNTIME_ENV_FILE", ""))
    if not runtime.is_absolute() or "$" in str(runtime):
        raise ReleaseError("explicit absolute runtime environment file is required")
    if _env_file(runtime, {"JUYA_ENVIRONMENT"}).get("JUYA_ENVIRONMENT") != profile["environment"]:
        raise ReleaseError("server runtime environment mismatch")
    url = profile.get("health_url", "")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ReleaseError("explicit health URL is required")
    return root


def _run(args, *, env=None, stdout=None):
    try:
        options = {"env": env, "stderr": subprocess.PIPE, "check": False}
        if stdout is None:
            options.update(stdout=subprocess.PIPE, text=True, encoding="utf-8")
        else:
            options["stdout"] = stdout
        process = subprocess.run(args, **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseError("server command could not execute") from error
    if process.returncode:
        # Docker/Compose errors can embed env values. Report stage, never stderr.
        raise ReleaseError("server command failed (exit " + str(process.returncode) + ")")
    return process.stdout or ""


def _inspect(container):
    try:
        result = json.loads(_run(["docker", "inspect", container]))
        if len(result) != 1:
            raise ValueError
        return result[0]
    except (ValueError, TypeError, KeyError) as error:
        raise ReleaseError("invalid container inspection result") from error


class _Compose:
    def __init__(self, directory, profile, manifest):
        self.profile = profile
        self.manifest = manifest
        self.directory = directory
        self.image = manifest.get("rollback_image_id", manifest["image_ref"])
        self.accepted_ids = None
        self.args = [
            "docker",
            "compose",
            "--env-file",
            profile["compose_env_file"],
            "-p",
            profile["compose_project"],
        ]
        for filename in profile["compose_files"]:
            path = directory / "deploy" / filename
            if not path.is_file() or path.is_symlink():
                raise ReleaseError("release Compose file is unavailable")
            self.args.extend(["-f", str(path)])
        for filename in profile.get("compose_extra_files", []):
            self.args.extend(["-f", filename])
        # Prevent inherited shell variables overriding the selected server env file.
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("JUYA_", "COMPOSE_"))
        }
        variable = (
            "JUYA_ADMIN_API_IMAGE"
            if profile["project"] == "juya-admin-api"
            else ("JUYA_MINIAPP_API_IMAGE")
        )
        self.variable = variable
        self.env[variable] = self.image

    def pin(self, immutable):
        self.image = immutable
        self.env[self.variable] = immutable

    def run(self, *args, stdout=None):
        return _run([*self.args, *args], env=self.env, stdout=stdout)

    def preflight(self):
        try:
            config = json.loads(self.run("config", "--format", "json"))
            if config.get("name") != self.profile["compose_project"]:
                raise ReleaseError("rendered Compose project mismatch")
            services = config["services"]
            selected = [*self.profile["services"]]
            if self.profile["migration_service"]:
                selected.append(self.profile["migration_service"])
            for name in selected:
                service = services[name]
                if service.get("image") != self.image:
                    raise ReleaseError("rendered Compose image mismatch")
                if (
                    service.get("environment", {}).get("JUYA_ENVIRONMENT")
                    != self.profile["environment"]
                ):
                    raise ReleaseError("rendered Compose environment mismatch")
            return config
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise ReleaseError("rendered Compose config is invalid") from error

    def container(self, service):
        containers = self.run("ps", "-q", service).split()
        if len(containers) > 1:
            raise ReleaseError("application service must have exactly one instance")
        return containers[0] if containers else None

    def runtime_preflight(self):
        package = self.profile["project"].replace("-", "_")
        code = (
            "import sys; from " + package + ".infrastructure.config import Settings; "
            "s=Settings(); assert s.environment == sys.argv[1], 'runtime environment mismatch'; "
            "assert not getattr(s, 'local_dev_mode', False), 'local mode is forbidden'; "
        )
        if self.profile["project"] == "juya-miniapp-api":
            code += (
                "assert s.application_configured(), 'application config incomplete'; "
                "from juya_miniapp_api.main import app; p=app.openapi()['paths']; "
                "assert '/api/v1/session/wechat' in p and '/api/v1/me' in p, "
                "'real routes required'; "
                "import asyncio; "
                "\nasync def check():\n"
                "    r=app.state.runtime_resources\n"
                "    try:\n"
                "        checks=await r.readiness()\n"
                "        assert checks and all(checks.values()), 'dependency readiness failed'\n"
                "    finally:\n"
                "        await r.close()\n"
                "asyncio.run(check())\n"
            )
        self.run(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            self.profile["api_service"],
            "-c",
            code,
            self.profile["environment"],
        )


def _load_image(directory, manifest):
    accepted = _image_identity(
        directory / "image.tar.gz",
        manifest["image_ref"],
        manifest["image_id"],
        untagged=manifest.get("legacy_snapshot", False),
    )
    _run(["docker", "load", "--input", str(directory / "image.tar.gz")])
    try:
        inspect_ref = (
            manifest["rollback_image_id"]
            if manifest.get("legacy_snapshot")
            else (manifest["image_ref"])
        )
        records = json.loads(_run(["docker", "image", "inspect", inspect_ref]))
        if len(records) != 1 or records[0].get("Id") not in accepted:
            raise ReleaseError("loaded image identity mismatch")
    except (ValueError, TypeError, KeyError) as error:
        raise ReleaseError("loaded image identity is unavailable") from error
    return accepted, records[0]["Id"]


def _release_link(root, name):
    path = root / name
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_symlink():
        raise ReleaseError("release state must use symbolic links")
    target = path.resolve(strict=True)
    releases = (root / "releases").resolve()
    if target.parent != releases:
        raise ReleaseError("release link escapes release root")
    return target


def _set_link(root, name, target):
    path = root / name
    if target is None:
        path.unlink(missing_ok=True)
        return
    if path.is_symlink() and path.resolve() == target:
        return
    temporary = root / ("." + name + "." + uuid4().hex + ".next")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        # Linux replaces directory symlinks atomically; Windows cannot overwrite them.
        if os.name == "nt" and path.is_symlink():
            path.unlink()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _install(bundle, root, manifest):
    releases = root / "releases"
    if releases.is_symlink():
        raise ReleaseError("release storage must not be a symlink")
    releases.mkdir(exist_ok=True)
    target = releases / manifest["release_id"]
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or (target / "SHA256SUMS").read_bytes() != (bundle / "SHA256SUMS").read_bytes()
        ):
            raise ReleaseError("release already exists with different content")
        _verify_files(target)
        return target
    temporary = releases / (".incoming-" + uuid4().hex)
    try:
        shutil.copytree(bundle, temporary)
        _verify_files(temporary)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def _volumes(config):
    return {name: (value or {}).get("name") for name, value in config.get("volumes", {}).items()}


def _current_runtime(compose):
    container = compose.container(compose.profile["api_service"])
    if not container:
        raise ReleaseError("current API container is missing")
    record = _inspect(container)
    env = record.get("Config", {}).get("Env", [])
    if "JUYA_ENVIRONMENT=" + compose.profile["environment"] not in env:
        raise ReleaseError("current container runtime environment mismatch")
    immutable = record.get("Image", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", immutable):
        raise ReleaseError("current container immutable image identity is unavailable")
    if record.get("Config", {}).get("Image") not in (
        compose.manifest["image_ref"],
        compose.image,
        immutable,
    ):
        raise ReleaseError("current container image does not match release")
    if (compose.directory / "image.tar.gz").is_file():
        compose.accepted_ids = _image_identity(
            compose.directory / "image.tar.gz",
            compose.manifest["image_ref"],
            compose.manifest["image_id"],
            untagged=compose.manifest.get("legacy_snapshot", False),
        )
        if immutable not in compose.accepted_ids:
            raise ReleaseError("current container immutable image does not match release")
    return immutable


def _write_checksums(directory):
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            entries.append(digest.hexdigest() + "  " + path.relative_to(directory).as_posix())
    (directory / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")


def _legacy_snapshot(directory, root, profile):
    """Upgrade retained old artifacts by saving the running immutable image before load."""
    _verify_files(directory, legacy=True)
    image = (directory / "image-ref.txt").read_text(encoding="utf-8").strip()
    image_id = (directory / "image-id.txt").read_text(encoding="utf-8").strip()
    prefix = profile["project"] + "-" + profile["environment"] + ":"
    commit = image.removeprefix(prefix)
    if (
        not image.startswith(prefix)
        or not re.fullmatch(r"[a-f0-9]{40}", commit)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
    ):
        raise ReleaseError("legacy image metadata cannot be safely converted")
    selected = {
        "schema_version": 1,
        "kind": "api",
        "project": profile["project"],
        "environment": profile["environment"],
        "commit": commit,
        "branch": "test",
        "image_ref": image,
        "image_id": image_id,
        "legacy_snapshot": True,
        "release_id": "legacy-" + commit[:12] + "-" + image_id[7:19],
    }
    old = _Compose(directory, profile, selected)
    old.preflight()
    immutable = _current_runtime(old)
    selected["rollback_image_id"] = immutable
    releases = root / "releases"
    if releases.is_symlink():
        raise ReleaseError("release storage must not be a symlink")
    releases.mkdir(exist_ok=True)
    target = releases / selected["release_id"]
    if not target.exists():
        temporary = releases / (".legacy-" + uuid4().hex)
        temporary.mkdir()
        try:
            shutil.copytree(directory / "deploy", temporary / "deploy")
            for filename in ("image-ref.txt", "image-id.txt"):
                shutil.copyfile(directory / filename, temporary / filename)
            raw = temporary / "saved-image.tar"
            _run(["docker", "save", "--output", str(raw), immutable])
            with (
                raw.open("rb") as source,
                gzip.open(temporary / "image.tar.gz", "wb", compresslevel=1) as destination,
            ):
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            raw.unlink()
            accepted = _image_identity(temporary / "image.tar.gz", image, image_id, untagged=True)
            if immutable not in accepted:
                raise ReleaseError("legacy saved image does not match running image")
            (temporary / "release.json").write_text(json.dumps(selected), encoding="utf-8")
            _write_checksums(temporary)
            _manifest(temporary, profile)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    saved = _manifest(target, profile)
    if saved != selected:
        raise ReleaseError("legacy snapshot already exists with different identity")
    return target, saved


def _backup(root, compose, release_id):
    backups = root / "backups"
    if backups.is_symlink():
        raise ReleaseError("database backup directory must not be a symlink")
    backups.mkdir(mode=0o700, exist_ok=True)
    filename = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + release_id + "-" + uuid4().hex
    target = backups / (filename + ".sql")
    # Password remains inside the MySQL container; neither arguments nor logs contain it.
    command = (
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot '
        "--single-transaction --routines --triggers --no-tablespaces juya"
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            compose.run("exec", "-T", "mysql", "sh", "-ec", command, stdout=stream)
            stream.flush()
            os.fsync(stream.fileno())
        if not target.stat().st_size:
            raise ReleaseError("database backup is empty")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _stop(compose):
    """Stop scheduling first and give workers a warm shutdown budget."""
    for service in compose.profile["beat_services"]:
        compose.run("stop", service)
    for service in compose.profile["worker_services"]:
        container = compose.container(service)
        compose.run("stop", "-t", "180", service)
        if container and _inspect(container).get("State", {}).get("ExitCode") == 137:
            raise ReleaseError("worker exceeded graceful shutdown budget")
    compose.run("stop", compose.profile["api_service"])


def _health(url):
    """Retry readiness while startup is in progress, with a bounded HTTP timeout."""
    for attempt in range(31):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    data = response.read(65537)
                    if len(data) <= 65536:
                        body = json.loads(data)
                        if isinstance(body, dict) and body.get("status") == "ready":
                            return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        if attempt < 30:
            time.sleep(2)
    raise ReleaseError("API readiness check failed")


def _healthy(compose, services):
    _health(compose.profile["health_url"])
    stable = 0
    for attempt in range(31):
        starting = False
        for service in services:
            container = compose.container(service)
            if not container:
                raise ReleaseError("application service is missing: " + service)
            record = _inspect(container)
            state = record.get("State", {})
            config = record.get("Config", {})
            if state.get("Status") != "running" or record.get("RestartCount") != 0:
                raise ReleaseError("application service did not start cleanly: " + service)
            if config.get("Image") != compose.image:
                raise ReleaseError("application service image mismatch: " + service)
            if compose.accepted_ids and record.get("Image") not in compose.accepted_ids:
                raise ReleaseError("application immutable image mismatch: " + service)
            if "JUYA_ENVIRONMENT=" + compose.profile["environment"] not in config.get("Env", []):
                raise ReleaseError("application service environment mismatch: " + service)
            health = state.get("Health", {}).get("Status")
            if health == "starting":
                starting = True
            elif health not in (None, "healthy"):
                raise ReleaseError("application container health failed: " + service)
        stable = 0 if starting else stable + 1
        if stable == 2:
            return
        if attempt < 30:
            time.sleep(2)
    raise ReleaseError("application container health timed out")


def _start(compose, services=None):
    services = compose.profile["services"] if services is None else services
    compose.run("up", "-d", "--no-deps", compose.profile["api_service"])
    _health(compose.profile["health_url"])
    background = [
        name
        for name in [*compose.profile["worker_services"], *compose.profile["beat_services"]]
        if name in services
    ]
    if background:
        compose.run("up", "-d", "--no-deps", *background)
    _healthy(compose, services)


@contextlib.contextmanager
def _lock(root):
    """Use the legacy shell flock filename so both deployment paths serialize."""
    path = root / ".artifact-deploy.lock"
    if path.is_symlink():
        raise ReleaseError("deployment lock must not be a symlink")
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ReleaseError("another deployment is running") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _interrupts():
    previous = {}
    if threading.current_thread() is threading.main_thread():

        def interrupted(signum, frame):
            raise KeyboardInterrupt("deployment interrupted")

        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.signal(number, interrupted)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def deploy_api(bundle: Path | None, profile: dict, manifest: dict | None, action="publish") -> dict:
    """Publish/rollback applications; database upgrades are never reversed."""
    if action not in ("publish", "rollback"):
        raise ReleaseError("unsupported API release action")
    root = _profile(profile)
    root.mkdir(parents=True, exist_ok=True)
    with _lock(root), _interrupts():
        current = _release_link(root, "current-release")
        previous = _release_link(root, "previous-release")
        old_directory = current
        if current and not (current / "release.json").is_file():
            old_directory, old_manifest = _legacy_snapshot(current, root, profile)
        else:
            old_manifest = _manifest(current, profile) if current else None
        old = _Compose(old_directory, profile, old_manifest) if current else None
        if action == "rollback":
            if bundle is not None or manifest is not None or current is None or previous is None:
                raise ReleaseError("rollback requires current and previous complete releases")
            candidate = previous
            selected = _manifest(candidate, profile)
        else:
            if bundle is None or manifest is None:
                raise ReleaseError("publish requires a complete bundle and manifest")
            bundle = Path(bundle)
            selected = _manifest(bundle, profile, manifest)
            # Collision preflight runs before docker load can replace an active tag.
            candidate = _install(bundle, root, selected)
        if current == candidate:
            raise ReleaseError("selected release is already current")
        target = _Compose(candidate, profile, selected)
        config = target.preflight()
        old_services = []
        if old:
            old_config = old.preflight()
            if _volumes(config) != _volumes(old_config):
                raise ReleaseError("release would change persistent Compose volumes")
            old.pin(_current_runtime(old))
            old_services = [name for name in profile["services"] if old.container(name)]
        elif profile["project"] == "juya-admin-api":
            raise ReleaseError("existing admin release is required; database bootstrap is separate")
        target.accepted_ids, loaded_id = _load_image(candidate, selected)
        if action == "rollback":
            target.pin(loaded_id)
        target.runtime_preflight()
        if action == "publish" and profile["project"] == "juya-admin-api":
            _backup(root, old, selected["release_id"])
        changed = False
        startup_attempted = False
        try:
            # Set this before stopping so partial stops and interrupts trigger recovery.
            changed = True
            if old:
                _stop(old)
            if action == "publish" and profile["migration_service"]:
                target.run("run", "--rm", "--no-deps", profile["migration_service"])
            startup_attempted = True
            _start(target)
            _set_link(root, "previous-release", old_directory)
            _set_link(root, "current-release", candidate)
        except BaseException as error:
            if changed:
                try:
                    if old:
                        # Stop candidate scheduling before restoring the earlier application.
                        if startup_attempted:
                            _stop(target)
                        _start(old, old_services)
                    else:
                        target.run(
                            "stop",
                            *profile["beat_services"],
                            *profile["worker_services"],
                            profile["api_service"],
                        )
                    _set_link(root, "current-release", current)
                    _set_link(root, "previous-release", previous)
                except BaseException as restore_error:
                    raise ReleaseError(
                        "release failed; application restore failed; manual recovery required"
                    ) from restore_error
            if isinstance(error, KeyboardInterrupt):
                raise ReleaseError(
                    "deployment interrupted; previous application restored"
                ) from error
            raise
        return {
            "project": profile["project"],
            "environment": profile["environment"],
            "action": action,
            "status": "deployed" if action == "publish" else "rolled_back",
            "release_id": selected["release_id"],
            "image_ref": selected["image_ref"],
            "previous_release_id": old_manifest["release_id"] if old_manifest else None,
            "database_downgraded": False,
        }
