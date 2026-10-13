"""Exercise release transactions with real files and a controlled Docker boundary."""

import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "deploy/local-release/api.py"


def write_bundle(directory, project="juya-admin-api", sequence="a", environment="test"):
    directory.mkdir(parents=True)
    (directory / "deploy").mkdir()
    (directory / "deploy/compose.yml").write_text("services: {}\n", encoding="utf-8")
    commit = sequence * 40
    image = f"{project}-{environment}:{commit}"
    config = json.dumps({"architecture": "amd64", "os": "linux", "fixture": sequence}).encode()
    image_id = "sha256:" + hashlib.sha256(config).hexdigest()
    oci = json.dumps({"schemaVersion": 2, "config": {"digest": image_id}, "layers": []}).encode()
    oci_id = "sha256:" + hashlib.sha256(oci).hexdigest()
    entries = {
        "manifest.json": json.dumps(
            [{"Config": image_id[7:] + ".json", "RepoTags": [image], "Layers": []}]
        ).encode(),
        image_id[7:] + ".json": config,
        "blobs/sha256/" + oci_id[7:]: oci,
        "index.json": json.dumps({"manifests": [{"digest": oci_id}]}).encode(),
    }
    with tarfile.open(directory / "image.tar.gz", "w:gz") as archive:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    manifest = {
        "schema_version": 1,
        "project": project,
        "environment": environment,
        "release_id": f"20261013-{sequence}",
        "commit": commit,
        "branch": "test",
        "kind": "api",
        "image_ref": image,
        "image_id": image_id,
    }
    (directory / "image-ref.txt").write_text(image + "\n", encoding="utf-8")
    (directory / "image-id.txt").write_text(image_id + "\n", encoding="utf-8")
    (directory / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    checksums(directory)
    return manifest, oci_id


def checksums(directory):
    lines = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        + "  "
        + path.relative_to(directory).as_posix()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


class DockerBoundary:
    """Model application versions and failures; never contact a Docker daemon."""

    def __init__(self, profile):
        self.profile = profile
        self.calls = []
        self.running = {}
        self.loaded_id = None
        self.fail_stage = None
        self.fail_image = None
        self.exit_code = 0
        self.migrations = 0
        self.environment = profile["environment"]
        self.saved_archive = None
        self.image_ids = {}
        self.stopped = set()
        self.starting_health = 0
        self.load_untagged = False
        self.inspected_images = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append((args, kwargs))
        env = kwargs.get("env") or {}
        image = env.get("JUYA_ADMIN_API_IMAGE", env.get("JUYA_MINIAPP_API_IMAGE", ""))
        output = ""
        status = 0
        if args[1:3] == ["image", "inspect"]:
            self.inspected_images.append(args[-1])
            identity = self.loaded_id
            if self.load_untagged and not args[-1].startswith("sha256:"):
                identity = "sha256:" + "f" * 64
            output = json.dumps([{"Id": identity}])
        elif args[1] == "save":
            Path(args[args.index("--output") + 1]).write_bytes(gzip.decompress(self.saved_archive))
        elif args[1] == "inspect":
            service = args[-1].removeprefix("container-")
            current_image = self.running.get(service, "")
            immutable = (
                current_image
                if current_image.startswith("sha256:")
                else self.image_ids.get(current_image, self.loaded_id)
            )
            output = json.dumps(
                [
                    {
                        "Id": args[-1],
                        "Image": immutable,
                        "Config": {
                            "Image": self.running.get(service),
                            "Env": ["JUYA_ENVIRONMENT=" + self.environment],
                        },
                        "State": {
                            "Status": "running",
                            "ExitCode": self.exit_code,
                            "Health": {"Status": "healthy"},
                        },
                        "RestartCount": 0,
                    }
                ]
            )
            if self.fail_stage == "restart" and self.running.get(service) == self.fail_image:
                output = output.replace('"RestartCount": 0', '"RestartCount": 1')
            if self.starting_health and current_image == self.fail_image:
                output = output.replace('"Status": "healthy"', '"Status": "starting"')
                self.starting_health -= 1
        elif args[1] == "compose":
            start = args.index("-f")
            while start < len(args) and args[start] == "-f":
                start += 2
            command = args[start:]
            if command[:1] == ["config"]:
                services = {
                    name: {
                        "image": image,
                        "environment": {"JUYA_ENVIRONMENT": self.environment},
                        "volumes": [],
                    }
                    for name in self.profile["services"]
                    + (
                        [self.profile["migration_service"]]
                        if self.profile["migration_service"]
                        else []
                    )
                }
                output = json.dumps(
                    {
                        "name": self.profile["compose_project"],
                        "services": services,
                        "volumes": {"mysql_data": {"name": "fixture_mysql"}},
                    }
                )
                if self.fail_stage == "volumes" and image == self.fail_image:
                    output = output.replace("fixture_mysql", "other_mysql")
                if self.fail_stage == "config":
                    status = 2
            elif command[:2] == ["ps", "-q"]:
                output = (
                    "container-" + command[-1]
                    if command[-1] in self.running and (command[-1] not in self.stopped)
                    else ""
                )
            elif command[:1] == ["exec"] and "mysqldump" in command[-1]:
                output = "-- fixture database backup\n"
                if self.fail_stage == "backup":
                    status = 2
            elif command[:1] == ["run"]:
                if "-c" in command:
                    compile(command[command.index("-c") + 1], "runtime-preflight", "exec")
                if command[-1] == self.profile["migration_service"]:
                    self.migrations += 1
                    if self.fail_stage == "migration" and image == self.fail_image:
                        status = 2
            elif command[:1] == ["stop"]:
                if self.fail_stage == "interrupt" and image == self.fail_image:
                    self.fail_stage = None
                    raise KeyboardInterrupt
                for name in command[1:]:
                    if name in self.profile["services"]:
                        self.stopped.add(name)
            elif command[:1] == ["up"]:
                for name in command[3:]:
                    self.running[name] = image
                    self.stopped.discard(name)
        if hasattr(kwargs.get("stdout"), "write"):
            kwargs["stdout"].write(output.encode())
            output = ""
        return subprocess.CompletedProcess(args, status, output, "fixture-sensitive-error")


@pytest.fixture
def release(tmp_path, monkeypatch):
    if MODULE.exists():
        spec = importlib.util.spec_from_file_location("local_api_release", MODULE)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
    else:

        def missing(*args, **kwargs):
            raise AssertionError("deploy_api is not implemented")

        api = SimpleNamespace(deploy_api=missing, subprocess=subprocess)
    server = tmp_path / "server"
    server.mkdir()
    runtime = tmp_path / "runtime.env"
    runtime.write_text("JUYA_ENVIRONMENT=test\n", encoding="utf-8")
    compose_env = tmp_path / "compose.env"
    compose_env.write_text("JUYA_RUNTIME_ENV_FILE=" + str(runtime) + "\n", encoding="utf-8")
    profile = {
        "project": "juya-admin-api",
        "environment": "test",
        "root": str(server),
        "health_url": "http://127.0.0.1:8000/health/ready",
        "compose_project": "existing-admin",
        "compose_env_file": str(compose_env),
        "compose_files": ["compose.yml"],
        "services": ["admin-api", "admin-worker", "admin-beat"],
        "api_service": "admin-api",
        "worker_services": ["admin-worker"],
        "beat_services": ["admin-beat"],
        "migration_service": "migrate",
    }
    docker = DockerBoundary(profile)
    monkeypatch.setattr(api.subprocess, "run", docker)
    if MODULE.exists():
        monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(api, "_health", lambda url: None)
    old = server / "releases/20261013-a"
    old_manifest, _ = write_bundle(old)
    (server / "current-release").symlink_to(old, target_is_directory=True)
    docker.running = dict.fromkeys(profile["services"], old_manifest["image_ref"])
    docker.image_ids[old_manifest["image_ref"]] = old_manifest["image_id"]
    docker.saved_archive = (old / "image.tar.gz").read_bytes()
    bundle = tmp_path / "bundle"
    manifest, oci_id = write_bundle(bundle, sequence="b")
    docker.loaded_id = manifest["image_id"]
    docker.image_ids[manifest["image_ref"]] = manifest["image_id"]
    return SimpleNamespace(
        api=api,
        server=server,
        bundle=bundle,
        manifest=manifest,
        profile=profile,
        docker=docker,
        old=old,
        runtime=runtime,
        oci_id=oci_id,
    )


def publish(release):
    return release.api.deploy_api(release.bundle, release.profile, release.manifest)


def previous_image_id(release):
    return json.loads((release.old / "release.json").read_text())["image_id"]


def test_admin_backs_up_before_migration_and_commits_release(release):
    result = publish(release)
    assert result["release_id"] == "20261013-b"
    assert release.server.joinpath("current-release").resolve().name == "20261013-b"
    assert release.server.joinpath("previous-release").resolve() == release.old
    assert next((release.server / "backups").glob("*.sql")).read_text().startswith("-- fixture")
    assert release.docker.migrations == 1
    assert set(release.docker.running.values()) == {release.manifest["image_ref"]}
    commands = [args for args, _ in release.docker.calls]
    assert next(i for i, args in enumerate(commands) if "mysqldump" in args[-1]) < next(
        i for i, args in enumerate(commands) if args[-1] == "migrate" and "run" in args
    )
    assert all("down" not in args and "--volumes" not in args for args in commands)
    assert all(
        args[args.index("-p") + 1] == "existing-admin" for args in commands if "compose" in args
    )


@pytest.mark.parametrize("stage", ["migration", "restart"])
def test_failed_publish_restores_application_and_keeps_current(release, stage):
    release.docker.fail_stage = stage
    release.docker.fail_image = release.manifest["image_ref"]
    with pytest.raises(RuntimeError):
        publish(release)
    assert release.server.joinpath("current-release").resolve() == release.old
    assert set(release.docker.running.values()) == {previous_image_id(release)}
    assert not release.server.joinpath("previous-release").exists()


def test_health_failure_restores_application(release, monkeypatch):
    calls = []

    def health(url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("readiness failed")

    if MODULE.exists():
        monkeypatch.setattr(release.api, "_health", health)
    with pytest.raises(RuntimeError):
        publish(release)
    assert release.server.joinpath("current-release").resolve() == release.old
    assert set(release.docker.running.values()) == {previous_image_id(release)}


def test_manual_rollback_validates_image_and_never_downgrades_database(release):
    publish(release)
    release.docker.loaded_id = json.loads((release.old / "release.json").read_text())["image_id"]
    result = release.api.deploy_api(None, release.profile, None, action="rollback")
    assert result["release_id"] == "20261013-a"
    assert release.server.joinpath("current-release").resolve() == release.old
    assert release.server.joinpath("previous-release").resolve().name == "20261013-b"
    assert release.docker.migrations == 1
    assert set(release.docker.running.values()) == {previous_image_id(release)}


def test_rejects_runtime_environment_before_docker(release):
    release.runtime.write_text("JUYA_ENVIRONMENT=production\n")
    with pytest.raises(RuntimeError, match="environment"):
        publish(release)
    assert not release.docker.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_ref", "other:test"),
        ("image_id", "sha256:" + "c" * 64),
        ("environment", "production"),
        ("project", "juya-miniapp-api"),
    ],
)
def test_manifest_mismatch_cannot_stop_old_application(release, field, value):
    release.manifest[field] = value
    with pytest.raises(RuntimeError):
        publish(release)
    assert not any("stop" in args for args, _ in release.docker.calls)


def test_accepts_oci_manifest_id_for_same_archive(release):
    release.docker.loaded_id = release.oci_id
    assert publish(release)["release_id"] == "20261013-b"


def test_wrong_loaded_image_blocks_database_backup(release):
    release.docker.loaded_id = "sha256:" + "c" * 64
    with pytest.raises(RuntimeError, match="image"):
        publish(release)
    assert not list((release.server / "backups").glob("*.sql"))
    assert not any("stop" in args for args, _ in release.docker.calls)


@pytest.mark.parametrize("stage", ["backup", "config"])
def test_preflight_failure_keeps_old_services_running(release, stage):
    release.docker.fail_stage = stage
    with pytest.raises(RuntimeError):
        publish(release)
    assert not any("stop" in args for args, _ in release.docker.calls)
    assert release.server.joinpath("current-release").resolve() == release.old


def test_forced_worker_exit_aborts_and_restores(release):
    release.docker.exit_code = 137
    with pytest.raises(RuntimeError, match="graceful"):
        publish(release)
    assert release.docker.migrations == 0
    assert set(release.docker.running.values()) == {previous_image_id(release)}


def test_legacy_release_is_converted_without_mutating_legacy_directory(release):
    (release.old / "release.json").unlink()
    (release.old / "image.tar.gz").unlink()
    result = publish(release)
    baseline = release.server.joinpath("previous-release").resolve()
    assert result["release_id"] == "20261013-b"
    assert baseline != release.old and baseline.name.startswith("legacy-")
    assert (baseline / "image.tar.gz").is_file() and (baseline / "release.json").is_file()
    assert not (release.old / "release.json").exists()
    assert not (release.old / "image.tar.gz").exists()
    saved = [args for args, _ in release.docker.calls if "save" in args]
    assert saved and saved[0][-1] == json.loads((baseline / "release.json").read_text())["image_id"]


def test_corrupt_previous_release_blocks_manual_rollback(release):
    publish(release)
    (release.old / "image.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError):
        release.api.deploy_api(None, release.profile, None, action="rollback")
    assert release.server.joinpath("current-release").resolve().name == "20261013-b"
    assert set(release.docker.running.values()) == {release.manifest["image_ref"]}


def test_miniapp_has_no_database_or_migration_actions(release, tmp_path):
    profile = release.profile
    profile.update(
        project="juya-miniapp-api",
        services=["miniapp-api", "miniapp-worker", "miniapp-beat"],
        api_service="miniapp-api",
        worker_services=["miniapp-worker"],
        beat_services=["miniapp-beat"],
        migration_service=None,
    )
    release.server.joinpath("current-release").unlink()
    release.docker.running = {}
    bundle = tmp_path / "miniapp-bundle"
    manifest, _ = write_bundle(bundle, project="juya-miniapp-api", sequence="c")
    release.docker.loaded_id = manifest["image_id"]
    result = release.api.deploy_api(bundle, profile, manifest)
    assert result["project"] == "juya-miniapp-api"
    assert set(release.docker.running.values()) == {manifest["image_ref"]}
    assert not release.docker.migrations
    assert not (release.server / "backups").exists()
    assert not any("mysqldump" in args[-1] or "down" in args for args, _ in release.docker.calls)


def test_release_id_collision_is_rejected_before_tag_load(release):
    target = release.server / "releases/20261013-b"
    write_bundle(target, sequence="c")
    with pytest.raises(RuntimeError, match="exists"):
        publish(release)
    assert not any("load" in args for args, _ in release.docker.calls)


def test_checksum_failure_is_rejected_before_docker(release):
    (release.bundle / "deploy/compose.yml").write_text("tampered")
    with pytest.raises(RuntimeError):
        publish(release)
    assert not release.docker.calls


def test_compose_environment_mismatch_cannot_stop_services(release):
    release.docker.environment = "production"
    with pytest.raises(RuntimeError, match="environment"):
        publish(release)
    assert not any("stop" in args for args, _ in release.docker.calls)


def test_preserves_absolute_server_override_on_every_compose_call(release, tmp_path):
    override = tmp_path / "runtime override.yml"
    override.write_text("services: {}\n")
    release.profile["compose_extra_files"] = [str(override)]
    publish(release)
    commands = [args for args, _ in release.docker.calls if "compose" in args]
    assert commands
    assert all(
        str(override) in args and args[args.index(str(override)) - 1] == "-f" for args in commands
    )


def test_missing_background_services_are_started_by_publish(release):
    release.docker.running.pop("admin-worker")
    release.docker.running.pop("admin-beat")
    publish(release)
    assert release.docker.running == dict.fromkeys(
        ["admin-api", "admin-worker", "admin-beat"], release.manifest["image_ref"]
    )


def test_changed_persistent_volume_is_rejected_before_stop(release):
    release.docker.fail_stage = "volumes"
    release.docker.fail_image = release.manifest["image_ref"]
    with pytest.raises(RuntimeError, match="volumes"):
        publish(release)
    assert not any("stop" in args or "load" in args for args, _ in release.docker.calls)


def test_interrupt_during_stop_restores_old_services(release):
    release.docker.fail_stage = "interrupt"
    release.docker.fail_image = previous_image_id(release)
    with pytest.raises(RuntimeError, match="interrupted"):
        publish(release)
    assert release.server.joinpath("current-release").resolve() == release.old
    assert set(release.docker.running.values()) == {previous_image_id(release)}


def test_failed_manual_rollback_restores_new_application(release):
    publish(release)
    old = json.loads((release.old / "release.json").read_text())
    release.docker.loaded_id = old["image_id"]
    release.docker.fail_image = old["image_id"]
    release.docker.fail_stage = "restart"
    with pytest.raises(RuntimeError):
        release.api.deploy_api(None, release.profile, None, action="rollback")
    assert release.server.joinpath("current-release").resolve().name == "20261013-b"
    assert release.server.joinpath("previous-release").resolve() == release.old
    assert set(release.docker.running.values()) == {release.manifest["image_id"]}
    assert release.docker.migrations == 1


def test_concurrent_publish_uses_existing_artifact_lock(release):
    with release.api._lock(release.server), pytest.raises(RuntimeError, match="another deployment"):
        publish(release)
    assert not release.docker.calls


@pytest.mark.parametrize("project", ["juya-admin-api", "juya-miniapp-api"])
@pytest.mark.parametrize("environment", ["test", "production"])
def test_backend_recipe_renders_selected_environment(tmp_path, project, environment):
    """Use the real Compose CLI: config must select environment and preserve service ownership."""
    repository = ROOT if project == "juya-admin-api" else ROOT.parent / project
    recipe = json.loads((repository / "deploy/local-release.json").read_text(encoding="utf-8"))
    assert recipe["project"] == project and recipe["kind"] == "api"
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose CLI is required for config rendering")
    runtime = tmp_path / "runtime.env"
    runtime.write_text("JUYA_ENVIRONMENT=" + environment + "\n")
    env = tmp_path / "compose.env"
    env.write_text(
        "\n".join(
            [
                "JUYA_RUNTIME_ENV_FILE=" + runtime.as_posix(),
                "JUYA_ADMIN_API_IMAGE=fixture-admin:release",
                "JUYA_MINIAPP_API_IMAGE=fixture-miniapp:release",
                "JUYA_MYSQL_ROOT_PASSWORD=fixture",
                "JUYA_MYSQL_APP_PASSWORD=fixture",
                "JUYA_MYSQL_MIGRATION_PASSWORD=fixture",
                "JUYA_OSS_EXPECTED_BUCKET=fixture-" + environment,
                "JUYA_SHARED_NETWORK=fixture-" + environment + "_default",
                "JUYA_ADMIN_API_PORT=18000",
                "JUYA_MINIAPP_API_PORT=18001",
            ]
        )
    )
    args = [docker, "compose", "--env-file", str(env)]
    for filename in recipe["compose_files"][environment]:
        args.extend(["-f", str(repository / "deploy" / filename)])
    process = subprocess.run(
        [*args, "config", "--format", "json"], capture_output=True, text=True, encoding="utf-8"
    )
    assert process.returncode == 0, process.stderr
    config = json.loads(process.stdout)
    for service in recipe["services"]:
        assert config["services"][service]["environment"]["JUYA_ENVIRONMENT"] == environment
    if project == "juya-miniapp-api":
        assert not config.get("volumes")
        assert "mysql" not in config["services"] and recipe["migration_service"] is None
        if environment == "production":
            assert config["networks"]["shared"]["name"] == "fixture-production_default"
            assert config["services"]["miniapp-api"]["environment"]["JUYA_OSS_EXPECTED_BUCKET"] == (
                "fixture-production"
            )
    else:
        assert set(config["volumes"]) == {"mysql_data", "redis_data", "juya_imports"}
        assert recipe["migration_service"] == "migrate"


def test_same_tag_failure_restores_captured_immutable_image_id(release):
    old_manifest = json.loads((release.old / "release.json").read_text())
    release.manifest["image_ref"] = old_manifest["image_ref"]
    (release.bundle / "release.json").write_text(json.dumps(release.manifest))
    (release.bundle / "image-ref.txt").write_text(old_manifest["image_ref"] + "\n")
    # Use another config under the same ref, reproducing rebuilds at an unchanged Git SHA.
    with tarfile.open(release.bundle / "image.tar.gz", "r:gz") as archive:
        entries = {member.name: archive.extractfile(member).read() for member in archive}
    records = json.loads(entries["manifest.json"])
    records[0]["RepoTags"] = [old_manifest["image_ref"]]
    entries["manifest.json"] = json.dumps(records).encode()
    with tarfile.open(release.bundle / "image.tar.gz", "w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    checksums(release.bundle)
    release.docker.fail_image = old_manifest["image_ref"]
    release.docker.fail_stage = "restart"
    with pytest.raises(RuntimeError):
        publish(release)
    assert set(release.docker.running.values()) == {old_manifest["image_id"]}
    assert release.server.joinpath("current-release").resolve() == release.old


def test_failure_preserves_previously_stopped_background_services(release):
    release.docker.stopped.update(["admin-worker", "admin-beat"])
    release.docker.fail_stage = "restart"
    release.docker.fail_image = release.manifest["image_ref"]
    with pytest.raises(RuntimeError):
        publish(release)
    assert release.docker.stopped == {"admin-worker", "admin-beat"}
    assert release.docker.running["admin-api"] == previous_image_id(release)


def test_waits_for_container_health_after_http_ready(release):
    release.docker.starting_health = 2
    release.docker.fail_image = release.manifest["image_ref"]
    assert publish(release)["release_id"] == "20261013-b"


@pytest.mark.parametrize("project", ["juya-admin-api", "juya-miniapp-api"])
def test_production_compose_requires_explicit_api_port(tmp_path, project):
    repository = ROOT if project == "juya-admin-api" else ROOT.parent / project
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose CLI is required")
    runtime = tmp_path / "runtime.env"
    runtime.write_text("JUYA_ENVIRONMENT=production\n")
    env = tmp_path / "compose.env"
    env.write_text(
        "\n".join(
            [
                "JUYA_RUNTIME_ENV_FILE=" + runtime.as_posix(),
                "JUYA_ADMIN_API_IMAGE=fixture-admin:release",
                "JUYA_MINIAPP_API_IMAGE=fixture-miniapp:release",
                "JUYA_MYSQL_ROOT_PASSWORD=fixture",
                "JUYA_MYSQL_APP_PASSWORD=fixture",
                "JUYA_MYSQL_MIGRATION_PASSWORD=fixture",
                "JUYA_OSS_EXPECTED_BUCKET=fixture-production",
                "JUYA_SHARED_NETWORK=fixture-production_default",
            ]
        )
    )
    process = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env),
            "-f",
            str(repository / "deploy/docker-compose.ecs-production.yml"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode != 0


def test_legacy_untagged_snapshot_rolls_back_after_mutable_tag_changes(release):
    old_manifest = json.loads((release.old / "release.json").read_text())
    with tarfile.open(fileobj=io.BytesIO(release.docker.saved_archive), mode="r:gz") as archive:
        entries = {member.name: archive.extractfile(member).read() for member in archive}
    records = json.loads(entries["manifest.json"])
    records[0]["RepoTags"] = None
    entries["manifest.json"] = json.dumps(records).encode()
    saved = io.BytesIO()
    with tarfile.open(fileobj=saved, mode="w:gz") as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    release.docker.saved_archive = saved.getvalue()
    (release.old / "release.json").unlink()
    (release.old / "image.tar.gz").unlink()
    publish(release)
    release.docker.loaded_id = old_manifest["image_id"]
    release.docker.load_untagged = True
    result = release.api.deploy_api(None, release.profile, None, action="rollback")
    assert result["release_id"].startswith("legacy-")
    assert release.docker.inspected_images[-1] == old_manifest["image_id"]
    assert set(release.docker.running.values()) == {old_manifest["image_id"]}


def test_http_200_without_ready_json_fails_health_check(release, monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *args):
            return b'{"status":"unavailable"}'

    monkeypatch.setattr(release.api.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    # Fixture bypasses HTTP for transactions; reload the real helper for this boundary test.
    spec = importlib.util.spec_from_file_location("real_api_health", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="readiness"):
        module._health(release.profile["health_url"])
