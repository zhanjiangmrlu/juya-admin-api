"""Exercise release integrity and environment separation with real files."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from tests.unit.deploy.local_release_testkit import write_config

MODULE = Path(__file__).resolve().parents[3] / "deploy/local-release/common.py"


def implementation():
    """Load the implementation without changing application import paths."""
    assert MODULE.is_file(), "local release common implementation is missing"
    spec = importlib.util.spec_from_file_location("local_release_common", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package(tmp_path, *, environment="test", extra=None, damaged=False):
    """Create an independently specified package and its transport digest."""
    manifest = {
        "schema_version": 1,
        "project": "juya-admin",
        "environment": environment,
        "kind": "web",
        "release_id": "20261013-abc123",
        "commit": "a" * 40,
        "branch": "test",
    }
    files = {"release.json": json.dumps(manifest).encode(), "site/index.html": b"<html>new</html>"}
    checksums = "".join(
        hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in files.items()
    )
    files["SHA256SUMS"] = checksums.encode()
    if damaged:
        files["site/index.html"] = b"modified after checksum"
    archive = tmp_path / "release.tgz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
        if extra:
            stream.addfile(extra, io.BytesIO(b"X") if extra.isfile() else None)
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_valid_archive_is_extracted_and_identity_returned(tmp_path):
    module = implementation()
    archive, digest = package(tmp_path)
    manifest = module.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "out")
    assert manifest["commit"] == "a" * 40
    assert (tmp_path / "out/site/index.html").read_bytes() == b"<html>new</html>"


@pytest.mark.parametrize("member", ["../outside", "/etc/overwritten", "site/../../outside"])
def test_unsafe_members_never_write_outside_destination(tmp_path, member):
    module = implementation()
    info = tarfile.TarInfo(member)
    info.size = 1
    archive, digest = package(tmp_path, extra=info)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "out")
    assert not (tmp_path / "outside").exists()


def test_symlink_cannot_redirect_release_extraction(tmp_path):
    module = implementation()
    link = tarfile.TarInfo("site/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    archive, digest = package(tmp_path, extra=link)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "out")


def test_transport_digest_failure_stops_before_extraction(tmp_path):
    module = implementation()
    archive, _ = package(tmp_path)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, "0" * 64, "juya-admin", "test", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_file_content_must_match_manifest_checksums(tmp_path):
    module = implementation()
    archive, digest = package(tmp_path, damaged=True)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "out")


def test_test_package_cannot_publish_to_production(tmp_path):
    module = implementation()
    archive, digest = package(tmp_path)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, digest, "juya-admin", "production", tmp_path / "out")


def test_unlisted_file_is_not_accepted_in_release(tmp_path):
    module = implementation()
    info = tarfile.TarInfo("credential.txt")
    info.size = 1
    archive, digest = package(tmp_path, extra=info)
    with pytest.raises(ValueError):
        module.validate_bundle(archive, digest, "juya-admin", "test", tmp_path / "out")


def test_production_without_its_own_host_does_not_use_test_host(tmp_path):
    module = implementation()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "environments": {
                    "test": {"ssh": {"host": "test.example.com", "user": "root"}, "projects": {}},
                    "production": {"ssh": {"host": "", "user": "root"}, "projects": {}},
                },
            }
        )
    )
    with pytest.raises(ValueError):
        module.load_config(config, "production", tmp_path)


def test_same_host_production_cannot_overwrite_test_site(tmp_path):
    module = implementation()
    config = tmp_path / "config.json"
    web = {
        "kind": "web",
        "root": "/opt/juya/admin",
        "site_path": "/opt/juya/web",
        "site_url": "https://admin.example.com",
        "api_url": "https://api.example.com/health/ready",
        "expected_api_base": "https://api.example.com",
        "health_url": "http://127.0.0.1:8000/health/ready",
    }
    value = {
        "schema_version": 1,
        "environments": {
            name: {
                "ssh": {"host": "same.example.com", "user": "root"},
                "projects": {"juya-admin": dict(web)},
            }
            for name in ("test", "production")
        },
    }
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        module.load_config(config, "production", tmp_path)


@pytest.mark.parametrize(
    "case", ["path_alias", "cross_field", "nested_root", "double_slash", "host_case"]
)
def test_production_paths_cannot_alias_or_contain_any_test_target(tmp_path, case):
    module = implementation()
    path = write_config(tmp_path, production=True)
    value = json.loads(path.read_text())
    test = value["environments"]["test"]
    production = value["environments"]["production"]
    production["ssh"]["host"] = test["ssh"]["host"]
    test_root = test["projects"]["juya-admin-api"]["root"]
    if case == "host_case":
        production["ssh"]["host"] = test["ssh"]["host"].upper()
        production["projects"]["juya-miniapp-api"]["root"] = test_root
    elif case == "double_slash":
        production["projects"]["juya-miniapp-api"]["root"] = "/" + test_root
    elif case == "cross_field":
        production["projects"]["juya-admin"]["site_path"] = test_root
    else:
        suffix = "/." if case == "path_alias" else "/nested-production"
        production["projects"]["juya-miniapp-api"]["root"] = test_root + suffix
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        module.load_config(path, "production", tmp_path)
