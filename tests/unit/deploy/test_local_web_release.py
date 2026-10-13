"""Exercise the web release transaction against real temporary sites"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

MODULE_PATH = Path(__file__).resolve().parents[3] / "deploy/local-release/web.py"


def load_web() -> ModuleType:
    """Load the standalone ECS handler without importing application dependencies"""
    if not MODULE_PATH.is_file():
        pytest.fail("deploy_web has not been implemented")
    spec = importlib.util.spec_from_file_location("local_release_web", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WebHost:
    """Keep real release files while replacing only Nginx and network processes"""

    def __init__(self, directory: Path, monkeypatch: pytest.MonkeyPatch):
        """Use directory for isolated server files and monkeypatch for process boundaries"""
        self.directory = directory
        self.monkeypatch = monkeypatch
        self.site = directory / "live site"
        self.root = directory / "release root"
        self.profile = {
            "project": "juya-admin",
            "environment": "test",
            "root": str(self.root),
            "site_path": str(self.site),
            "site_url": "https://test-admin.juyayingyu.com",
            "page_path": "/login",
            "api_url": "https://test-admin-api.juyayingyu.com",
            "expected_api_base": "https://test-admin-api.juyayingyu.com",
            "health_url": "http://127.0.0.1:8000/health/ready",
        }
        self.failure = ""
        self.module = None

    def write_site(self, destination: Path, label: str) -> Path:
        """Create destination with label identifying independently checked HTML and JS"""
        (destination / "assets").mkdir(parents=True)
        (destination / "index.html").write_text(
            f'<title>{label}</title><script type="module" src="/assets/entry.js"></script>',
            encoding="utf-8",
        )
        (destination / "assets/entry.js").write_text(
            f'const release="{label}";const api="{self.profile["expected_api_base"]}";',
            encoding="utf-8",
        )
        return destination

    def process(self, arguments, **options):
        """Serve the actual current symlink target and inject one named external failure"""
        assert options.get("shell", False) is False
        assert isinstance(arguments, list)
        if arguments == ["nginx", "-t"]:
            if self.failure == "nginx":
                raise subprocess.CalledProcessError(1, arguments)
            return subprocess.CompletedProcess(arguments, 0, b"", b"")
        assert arguments[0] == "curl"
        assert "--fail" in arguments and "--max-time" in arguments
        assert "--insecure" not in arguments and "-k" not in arguments
        url = arguments[-1]
        if url.startswith("https://"):
            host = url.split("/", 3)[2]
            assert arguments[arguments.index("--resolve") + 1] == f"{host}:443:127.0.0.1"
            assert arguments[arguments.index("--noproxy") + 1] == "*"
            if self.failure == "tls":
                raise subprocess.CalledProcessError(60, arguments)
        if url == self.profile["health_url"]:
            output = (
                b'{"status":"starting"}' if self.failure == "backend" else b'{"status":"ready"}'
            )
        elif url == (
            self.profile["api_url"]
            if self.profile["api_url"].endswith("/health/ready")
            else self.profile["api_url"] + "/health/ready"
        ):
            output = b'{"status":"starting"}' if self.failure == "api" else b'{"status":"ready"}'
        elif url == self.profile["site_url"] + self.profile["page_path"]:
            if self.failure == "interrupt":
                raise KeyboardInterrupt
            if self.failure == "terminate":
                signal.raise_signal(signal.SIGTERM)
            output = b"WRONG" if self.failure == "page" else (self.site / "index.html").read_bytes()
        elif url == self.profile["site_url"] + "/assets/entry.js":
            output = (
                b"WRONG"
                if self.failure == "entry"
                else (self.site / "assets/entry.js").read_bytes()
            )
        else:
            pytest.fail(f"Unexpected request: {url}")
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    def deploy(self, label: str, action: str = "publish") -> dict:
        """Run the real handler with label as a unique release or action as manual rollback"""
        if self.module is None:
            self.module = load_web()
            self.monkeypatch.setattr(self.module.subprocess, "run", self.process)
        if action == "rollback":
            return self.module.deploy_web(None, self.profile, None, action="rollback")
        bundle = self.directory / f"bundle-{label}"
        self.write_site(bundle / "site", label)
        manifest = {
            "schema_version": 1,
            "project": self.profile["project"],
            "environment": self.profile["environment"],
            "release_id": label,
            "kind": "web",
        }
        return self.module.deploy_web(bundle, self.profile, manifest)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Provide an isolated host using pytest temporary files and process patching"""
    if os.name == "nt":
        pytest.skip("ECS atomic directory-symlink replacement requires POSIX; run on Linux")
    return WebHost(tmp_path, monkeypatch)


def test_publish_preserves_directory_and_previous_symlink_target(host):
    """A second publication must retain both legacy files and the first release"""
    host.write_site(host.site, "legacy")
    result = host.deploy("one")
    first_target = host.site.resolve()
    assert result["action"] == "publish"
    assert host.site.is_symlink()
    assert "one" in (host.site / "index.html").read_text()
    assert any("legacy" in p.read_text() for p in host.root.glob("backups/**/index.html"))
    host.deploy("two")
    assert "two" in (host.site / "index.html").read_text()
    assert "one" in (first_target / "index.html").read_text()


@pytest.mark.parametrize("failure", ["nginx", "backend", "page", "entry", "api", "tls"])
def test_failed_publish_restores_old_directory_and_never_records_success(host, failure):
    """Each external validation failure must leave the prior application available"""
    host.write_site(host.site, "legacy")
    host.failure = failure
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        host.deploy("broken")
    assert "legacy" in (host.site / "index.html").read_text()
    assert not (host.root / "web-state.json").exists()


def test_failed_publish_restores_original_relative_symlink(host):
    """Recovery must preserve a previous relative symlink and its files"""
    old = host.write_site(host.directory / "old release", "legacy")
    host.site.symlink_to(old.relative_to(host.directory), target_is_directory=True)
    host.failure = "entry"
    with pytest.raises(RuntimeError):
        host.deploy("broken")
    assert os.readlink(host.site) == "old release"
    assert "legacy" in (old / "index.html").read_text()


def test_first_publication_failure_removes_unverified_live_link(host):
    """A failed first release must leave no live website pointing to unverified files"""
    host.failure = "page"
    with pytest.raises(RuntimeError):
        host.deploy("broken")
    assert not host.site.exists() and not host.site.is_symlink()


def test_interrupt_during_validation_restores_live_directory(host):
    """Keyboard interruption must run the same restoration as an HTTP failure"""
    host.write_site(host.site, "legacy")
    host.failure = "interrupt"
    with pytest.raises(KeyboardInterrupt):
        host.deploy("interrupted")
    assert "legacy" in (host.site / "index.html").read_text()


def test_manual_rollback_checks_target_and_allows_returning_to_current(host):
    """Manual rollback must verify the prior release and preserve the replaced release"""
    host.deploy("one")
    host.deploy("two")
    result = host.deploy("unused", action="rollback")
    assert result["action"] == "rollback"
    assert "one" in (host.site / "index.html").read_text()
    host.deploy("unused", action="rollback")
    assert "two" in (host.site / "index.html").read_text()


@pytest.mark.parametrize("failure", ["page", "entry", "api", "tls"])
def test_failed_manual_rollback_restores_current_link_and_state(host, failure):
    """A failed rollback must not strand the site at the older unverified release"""
    host.deploy("one")
    host.deploy("two")
    current = os.readlink(host.site)
    state = (host.root / "web-state.json").read_bytes()
    host.failure = failure
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        host.deploy("unused", action="rollback")
    assert os.readlink(host.site) == current
    assert (host.root / "web-state.json").read_bytes() == state
    assert "two" in (host.site / "index.html").read_text()


def test_rollback_without_previous_release_does_not_change_current(host):
    """The first release cannot roll back to a nonexistent previous site"""
    host.deploy("one")
    with pytest.raises(RuntimeError, match="previous"):
        host.deploy("unused", action="rollback")
    assert "one" in (host.site / "index.html").read_text()


@pytest.mark.parametrize("environment", ["test", "production"])
def test_api_bundle_must_match_explicit_environment_before_switch(host, environment):
    """A build for another API host must be rejected before the old site is touched"""
    host.profile["environment"] = environment
    if environment == "production":
        host.profile.update(
            site_url="https://admin.juyayingyu.com",
            api_url="https://admin-api.juyayingyu.com",
            expected_api_base="https://admin-api.juyayingyu.com",
        )
    host.write_site(host.site, "legacy")
    module = load_web()
    host.monkeypatch.setattr(module.subprocess, "run", host.process)
    bundle = host.directory / "wrong-api"
    site = host.write_site(bundle / "site", "wrong")
    (site / "assets/entry.js").write_text('const api="https://different-api.example";')
    with pytest.raises(RuntimeError, match="API"):
        module.deploy_web(bundle, host.profile, {"release_id": "wrong"})
    assert "legacy" in (host.site / "index.html").read_text()


@pytest.mark.parametrize(
    "source", ["https://evil.example/entry.js", "/assets/../entry.js", "/missing.js"]
)
def test_invalid_entry_path_never_switches_live_site(host, source):
    """Remote and traversing entry scripts must not be accepted as the bundle entry"""
    module = load_web()
    host.write_site(host.site, "legacy")
    bundle = host.directory / "invalid-entry"
    site = host.write_site(bundle / "site", "invalid")
    (site / "index.html").write_text(f'<script src="{source}"></script>')
    with pytest.raises(RuntimeError, match="entry"):
        module.deploy_web(bundle, host.profile, {"release_id": "bad"})
    assert "legacy" in (host.site / "index.html").read_text()


def test_h5_uses_profile_domain_page_and_api_readiness(host):
    """H5 must publish site/ and validate the independently configured production URLs"""
    host.profile.update(
        project="juya-miniapp-h5",
        environment="production",
        site_url="https://h5.juyayingyu.com",
        api_url="https://h5.juyayingyu.com",
        expected_api_base="https://h5.juyayingyu.com",
        page_path="/",
        health_url="http://127.0.0.1:8001/health/ready",
    )
    host.deploy("h5-prod")
    assert "h5-prod" in (host.site / "index.html").read_text()


def test_profile_api_url_can_be_the_explicit_public_readiness_endpoint(host):
    """The configured public readiness URL must be used without appending its path twice"""
    host.profile["api_url"] += "/health/ready"
    host.deploy("explicit-ready")
    assert "explicit-ready" in (host.site / "index.html").read_text()


def test_public_readiness_can_use_independent_api_host_for_proxied_h5(host):
    """A site-origin API build may validate readiness through its separately configured API host"""
    host.profile["api_url"] = "https://test-api.juyayingyu.com/health/ready"
    host.deploy("independent-ready")
    assert "independent-ready" in (host.site / "index.html").read_text()


def test_state_failure_restores_current_release(host):
    """An unwritable state target must restore the site after successful HTTP checks"""
    host.write_site(host.site, "legacy")
    host.root.mkdir()
    (host.root / "web-state.json").mkdir()
    with pytest.raises(OSError):
        host.deploy("one")
    assert "legacy" in (host.site / "index.html").read_text()


def test_reused_release_id_cannot_overwrite_a_retained_release(host):
    """Duplicate identifiers must stop before changing files used by the current site"""
    host.deploy("one")
    module = host.module
    with pytest.raises((RuntimeError, FileExistsError)):
        module.deploy_web(host.directory / "bundle-one", host.profile, {"release_id": "one"})
    assert "one" in (host.site / "index.html").read_text()
    assert json.loads((host.root / "web-state.json").read_text())["current"]["release_id"] == "one"


def test_another_process_holding_lock_prevents_switch_then_releases_lock(host):
    """A real lock held by another process must block publishing without changing the site"""
    module = load_web()
    host.write_site(host.site, "legacy")
    child = (
        "import importlib.util,sys;from pathlib import Path;"
        "s=importlib.util.spec_from_file_location('web',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "c=m._release_lock(Path(sys.argv[2]));c.__enter__();"
        "print('locked',flush=True);sys.stdin.readline();c.__exit__(None,None,None)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", child, str(MODULE_PATH), str(host.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert process.stdout.readline().strip() == "locked"
            with pytest.raises(RuntimeError, match="lock"):
                module.deploy_web(host.directory, host.profile, {"release_id": "blocked"})
            assert "legacy" in (host.site / "index.html").read_text()
        finally:
            process.communicate("\n", timeout=10)
    host.deploy("released")
    assert "released" in (host.site / "index.html").read_text()


def test_existing_cloud_deploy_lock_also_blocks_local_release(host):
    """The local handler must respect the lock file used by the unchanged cloud script"""
    import fcntl

    host.write_site(host.site, "legacy")
    legacy_lock = host.site.parent / f".{host.site.name}-deploy.lock"
    with legacy_lock.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="lock"):
            host.deploy("cloud-locked")
        assert "legacy" in (host.site / "index.html").read_text()


def test_sigterm_restores_site_and_callers_signal_handler(host):
    """SIGTERM during HTTP validation must restore the old site and original handler"""
    previous_handler = signal.getsignal(signal.SIGTERM)
    host.write_site(host.site, "legacy")
    host.failure = "terminate"
    with pytest.raises(InterruptedError):
        host.deploy("terminated")
    assert "legacy" in (host.site / "index.html").read_text()
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_interrupt_immediately_after_symlink_switch_restores_current(host):
    """Interruption between the atomic rename and its return must not lose the old link"""
    host.deploy("one")
    module = host.module
    original_link = module._atomic_link
    switched = False

    def interrupted_link(destination, target):
        """Replace destination with target and interrupt once after the real rename"""
        nonlocal switched
        original_link(destination, target)
        if not switched:
            switched = True
            raise InterruptedError("Interrupted after rename")

    host.monkeypatch.setattr(module, "_atomic_link", interrupted_link)
    with pytest.raises(InterruptedError):
        host.deploy("two")
    assert "one" in (host.site / "index.html").read_text()
    assert json.loads((host.root / "web-state.json").read_text())["current"]["release_id"] == "one"


def test_interrupt_after_state_write_restores_old_state_and_live_site(host):
    """Interruption immediately after state commit must restore both the link and saved state"""
    host.deploy("one")
    old_state = (host.root / "web-state.json").read_bytes()
    original_write = host.module._write_state

    def interrupted_state(path, value):
        """Write actual state value to path and simulate an interruption at the commit boundary"""
        original_write(path, value)
        raise InterruptedError("Interrupted after state commit")

    host.monkeypatch.setattr(host.module, "_write_state", interrupted_state)
    with pytest.raises(InterruptedError):
        host.deploy("two")
    assert "one" in (host.site / "index.html").read_text()
    assert (host.root / "web-state.json").read_bytes() == old_state


@pytest.mark.parametrize("field", ["site_url", "api_url", "expected_api_base"])
def test_production_requires_every_explicit_domain(host, field):
    """Missing production endpoint fields must not fall back to any test target"""
    host.profile["environment"] = "production"
    host.write_site(host.site, "legacy")
    del host.profile[field]
    with pytest.raises(RuntimeError, match="profile"):
        load_web().deploy_web(host.directory, host.profile, {"release_id": "missing-domain"})
    assert "legacy" in (host.site / "index.html").read_text()


def test_api_hostname_prefix_cannot_satisfy_bundle_validation(host):
    """A longer unrelated hostname must not pass the expected API origin check"""
    module = load_web()
    host.write_site(host.site, "legacy")
    bundle = host.directory / "prefix"
    site = host.write_site(bundle / "site", "invalid")
    (site / "assets/entry.js").write_text(
        'const api="https://test-admin-api.juyayingyu.com.evil.example";'
    )
    with pytest.raises(RuntimeError, match="API"):
        module.deploy_web(bundle, host.profile, {"release_id": "prefix"})
    assert "legacy" in (host.site / "index.html").read_text()


def test_manifest_environment_mismatch_is_rejected_before_switch(host):
    """A manifest for another environment must stop before any filesystem transition"""
    module = load_web()
    host.write_site(host.site, "legacy")
    with pytest.raises(RuntimeError, match="environment"):
        module.deploy_web(
            host.directory,
            host.profile,
            {
                "release_id": "wrong",
                "environment": "production",
            },
        )
    assert "legacy" in (host.site / "index.html").read_text()


def test_missing_entry_file_is_rejected_before_switch(host):
    """A reference to a missing local asset must never become the live website"""
    module = load_web()
    host.write_site(host.site, "legacy")
    bundle = host.directory / "missing"
    site = host.write_site(bundle / "site", "invalid")
    (site / "assets/entry.js").unlink()
    with pytest.raises(RuntimeError, match="entry"):
        module.deploy_web(bundle, host.profile, {"release_id": "missing"})
    assert "legacy" in (host.site / "index.html").read_text()


def test_rollback_refuses_state_after_an_external_site_change(host):
    """Saved state must not roll back a live link replaced by an unrelated deployment"""
    host.deploy("one")
    host.deploy("two")
    external = host.write_site(host.directory / "external", "external")
    host.site.unlink()
    host.site.symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="differs"):
        host.deploy("unused", action="rollback")
    assert "external" in (host.site / "index.html").read_text()


def test_state_from_another_environment_cannot_be_used_for_rollback(host):
    """A wrong environment in saved state must stop before replacing the current release"""
    host.deploy("one")
    host.deploy("two")
    state_path = host.root / "web-state.json"
    state = json.loads(state_path.read_text())
    state["environment"] = "production"
    state_path.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match="identity"):
        host.deploy("unused", action="rollback")
    assert "two" in (host.site / "index.html").read_text()


def test_existing_legacy_symlink_is_saved_as_manual_rollback_target(host):
    """The first managed release must retain a previous release outside the new root"""
    old = host.write_site(host.directory / "cloud release", "legacy")
    host.site.symlink_to(old, target_is_directory=True)
    host.deploy("first-local")
    state = json.loads((host.root / "web-state.json").read_text())
    assert state["previous"]["site"] == str(old)
    host.deploy("unused", action="rollback")
    assert host.site.resolve() == old
    assert "legacy" in (host.site / "index.html").read_text()


def test_remote_private_umask_does_not_hide_static_release_or_backups(host):
    """A remote entry umask of 077 must still leave only owned static ancestors traversable"""
    host.root = host.directory / "new managed root" / "admin-test"
    host.profile["root"] = str(host.root)
    host.write_site(host.site, "legacy")
    legacy_mode = host.site.stat().st_mode & 0o777
    existing_parent_mode = host.directory.stat().st_mode & 0o777
    previous_umask = os.umask(0o077)
    try:
        host.deploy("one")
    finally:
        os.umask(previous_umask)
    for directory in (
        host.root.parent,
        host.root,
        host.root / "releases",
        host.root / "releases/one",
        host.root / "backups",
        host.site.resolve(),
    ):
        assert directory.stat().st_mode & 0o777 == 0o755
    assert host.directory.stat().st_mode & 0o777 == existing_parent_mode
    backup = next((host.root / "backups").iterdir())
    assert backup.stat().st_mode & 0o777 == legacy_mode
    assert (host.root / "web-state.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("repository", "project"), [("juya-admin", "juya-admin"), ("juya-miniapp", "juya-miniapp-h5")]
)
def test_project_wrapper_forwards_environment_action_and_config_with_spaces(
    tmp_path, repository, project
):
    """Invoke the wrapper and observe arguments received by an isolated workspace entry"""
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required for the local Windows wrapper test")
    wrapper = MODULE_PATH.parents[3] / repository / "deploy/publish.ps1"
    if not wrapper.is_file():
        pytest.fail(f"Project wrapper missing: {repository}")
    entry = tmp_path / "publish.ps1"
    entry.write_text(
        "param([string[]]$Project,[string]$Environment,[string]$Action,[string]$Config,"
        "[switch]$BuildOnly)\n"
        "[ordered]@{project=$Project;environment=$Environment;action=$Action;"
        "config=$Config;build_only=$BuildOnly.IsPresent}|ConvertTo-Json -Compress\n"
        "exit 0\n",
        encoding="utf-8",
    )
    local_wrapper = tmp_path / repository / "deploy/publish.ps1"
    local_wrapper.parent.mkdir(parents=True)
    shutil.copyfile(wrapper, local_wrapper)
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(local_wrapper),
            "-Environment",
            "production",
            "-Action",
            "plan",
            "-Config",
            "C:\\local config\\target.json",
            "-BuildOnly",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == {
        "project": [project],
        "environment": "production",
        "action": "plan",
        "config": "C:\\local config\\target.json",
        "build_only": True,
    }


@pytest.mark.parametrize("repository", ["juya-admin", "juya-miniapp"])
def test_project_wrapper_propagates_workspace_failure_exit_code(tmp_path, repository):
    """A failed workspace command must remain failed for callers of the project wrapper"""
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required for the local Windows wrapper test")
    wrapper = MODULE_PATH.parents[3] / repository / "deploy/publish.ps1"
    if not wrapper.is_file():
        pytest.fail(f"Project wrapper missing: {repository}")
    (tmp_path / "publish.ps1").write_text(
        "param([string[]]$Project,[string]$Environment,[string]$Action)\nexit 17\n",
        encoding="utf-8",
    )
    local_wrapper = tmp_path / repository / "deploy/publish.ps1"
    local_wrapper.parent.mkdir(parents=True)
    shutil.copyfile(wrapper, local_wrapper)
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(local_wrapper), "-Action", "rollback"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 17
