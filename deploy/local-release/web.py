"""Publish verified static sites without changing the existing Nginx configuration"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class _EntryParser(HTMLParser):
    """Extract local script entries from the generated index page"""

    def __init__(self):
        """Collect script sources in document order"""
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        """Read script tag attributes from the generated HTML"""
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


def _https_base(value: str, field: str) -> str:
    """Validate value as an explicit HTTPS origin or base path identified by field"""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
        or "\\" in value
        or ".." in parsed.path.split("/")
    ):
        raise RuntimeError(f"Invalid explicit HTTPS {field}")
    return value.rstrip("/")


def _settings(profile: dict) -> dict:
    """Validate profile's environment, paths, page and explicit site/API endpoints"""
    required = (
        "project",
        "environment",
        "root",
        "site_path",
        "site_url",
        "api_url",
        "expected_api_base",
        "page_path",
        "health_url",
    )
    if any(not isinstance(profile.get(key), str) or not profile[key] for key in required):
        raise RuntimeError("Missing explicit web release profile field")
    if profile["environment"] not in ("test", "production"):
        raise RuntimeError("Unsupported release environment")
    result = dict(profile)
    result["root"] = Path(profile["root"])
    result["site_path"] = Path(profile["site_path"])
    if not result["root"].is_absolute() or not result["site_path"].is_absolute():
        raise RuntimeError("Web release paths must be absolute")
    if result["root"] == result["site_path"] or result["root"].is_relative_to(result["site_path"]):
        raise RuntimeError("Release root cannot be inside the live site")
    for field in ("site_url", "api_url", "expected_api_base"):
        result[field] = _https_base(profile[field], field)
    result["api_health_url"] = (
        result["api_url"]
        if result["api_url"] != result["expected_api_base"]
        else result["api_url"] + "/health/ready"
    )
    page = profile["page_path"]
    if (
        not page.startswith("/")
        or page.startswith("//")
        or any(character in page for character in "?#\\")
        or ".." in page.split("/")
    ):
        raise RuntimeError("Invalid page_path")
    health = urlsplit(profile["health_url"])
    if (
        health.scheme not in ("http", "https")
        or not health.hostname
        or health.username is not None
        or health.password is not None
    ):
        raise RuntimeError("Invalid readiness URL")
    return result


def _public_directory(path: Path, owned: bool = True):
    """Create traversable static ancestors for path, changing existing permissions only if owned"""
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        directory.chmod(0o755)
    if owned:
        path.chmod(0o755)


@contextlib.contextmanager
def _file_lock(path: Path):
    """Hold an OS process lock at path until the caller completes or fails"""
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another web release holds the lock") from error
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("Another web release holds the lock") from error
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _release_lock(root: Path):
    """Serialize managed release state and directories under the owned root"""
    _public_directory(root)
    with _file_lock(root / ".web-release.lock"):
        yield


@contextlib.contextmanager
def _site_lock(site: Path):
    """Respect the existing ecs-web-deploy lock beside site without changing old permissions"""
    _public_directory(site.parent, owned=False)
    with _file_lock(site.parent / f".{site.name}-deploy.lock"):
        yield


def _terminate(signum, frame):
    """Turn the received termination signal into an exception so restoration can run"""
    raise InterruptedError(f"Web release interrupted by signal {signum}")


@contextlib.contextmanager
def _interrupt_guard():
    """Restore the caller's SIGTERM handler after protecting the release transaction"""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    original = signal.signal(signal.SIGTERM, _terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, original)


def _validate_site(site: Path, expected_api_base: str) -> str:
    """Verify site's HTML, local entry script and expected API base before any switch"""
    if site.is_symlink() or not site.is_dir():
        raise RuntimeError("Missing static site directory")
    if any(item.is_symlink() for item in site.rglob("*")):
        raise RuntimeError("Static site contains a symlink")
    index = site / "index.html"
    assets = site / "assets"
    if not index.is_file() or not index.stat().st_size or not assets.is_dir():
        raise RuntimeError("Static site requires index.html and assets")
    parser = _EntryParser()
    parser.feed(index.read_text(encoding="utf-8"))
    source = next((item for item in parser.sources if item.endswith(".js")), "")
    path = PurePosixPath(source)
    if (
        not source.startswith("/assets/")
        or source.startswith("//")
        or ".." in path.parts
        or "\\" in source
        or "?" in source
        or "#" in source
    ):
        raise RuntimeError("Invalid local entry JS path")
    entry = site / source.lstrip("/")
    if not entry.is_file() or not entry.stat().st_size:
        raise RuntimeError("Missing entry JS file")
    # Use a delimiter boundary so an unrelated hostname prefix cannot satisfy the API check
    api_pattern = re.compile(re.escape(expected_api_base.encode()) + rb'(?=["\s\x27`/;,)\]}]|$)')
    if not any(api_pattern.search(item.read_bytes()) for item in assets.rglob("*.js")):
        raise RuntimeError(
            "Expected API base missing from site assets; rebuild for this environment"
        )
    return source


def _run(arguments: list[str]) -> bytes:
    """Execute arguments without a shell and retain bounded process failures"""
    result = subprocess.run(arguments, check=True, capture_output=True, timeout=20)
    return result.stdout


def _http(url: str) -> bytes:
    """Fetch url with normal TLS verification and local Nginx routing for HTTPS"""
    arguments = ["curl", "--fail", "--silent", "--show-error", "--max-time", "10"]
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        arguments += ["--noproxy", "*", "--resolve", f"{parsed.hostname}:443:127.0.0.1"]
    return _run([*arguments, url])


def _ready(url: str):
    """Require the API at url to return a JSON readiness status"""
    try:
        response = json.loads(_http(url))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("API readiness response is not JSON") from error
    if not isinstance(response, dict) or response.get("status") != "ready":
        raise RuntimeError("API is not ready")


def _verify_live(site: Path, profile: dict, entry: str):
    """Compare site's bytes against profile's HTTPS page and entry, then check the API"""
    if _http(profile["site_url"] + profile["page_path"]) != (site / "index.html").read_bytes():
        raise RuntimeError("HTTPS page does not match the release")
    if _http(profile["site_url"] + entry) != (site / entry.lstrip("/")).read_bytes():
        raise RuntimeError("HTTPS entry JS does not match the release")
    _ready(profile["api_health_url"])


def _atomic_link(destination: Path, target: str | Path):
    """Replace destination using a sibling temporary symlink pointing at target"""
    temporary = destination.with_name(f"{destination.name}.next-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, destination)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _snapshot(site: Path) -> dict:
    """Capture site's real directory or exact previous symlink for reliable restoration"""
    if site.is_symlink():
        target = site.resolve(strict=True)
        if not target.is_dir():
            raise RuntimeError("Previous website link does not point to a directory")
        return {"kind": "link", "link": os.readlink(site), "site": str(target)}
    if site.exists():
        if not site.is_dir():
            raise RuntimeError("Live website path is not a directory")
        return {"kind": "directory", "site": str(site)}
    return {"kind": "absent", "site": None}


def _restore(site: Path, original: dict, backup: Path | None):
    """Restore site's original shape and target from original and an optional directory backup"""
    if original["kind"] == "link":
        _atomic_link(site, original["link"])
    elif original["kind"] == "directory":
        if site.is_symlink():
            site.unlink()
        if backup is not None and backup.exists():
            os.replace(backup, site)
    elif site.is_symlink():
        site.unlink()


def _write_bytes(path: Path, content: bytes):
    """Write content to path atomically for committing or restoring release state"""
    temporary = path.with_name(f"{path.name}.next-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_state(path: Path, value: dict):
    """Commit the verified release state value at path using an atomic byte replacement"""
    _write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def deploy_web(
    bundle: Path | None, profile: dict, manifest: dict | None, action: str = "publish"
) -> dict:
    """Publish bundle using profile and manifest, or verify and switch the saved rollback target"""
    if action not in ("publish", "rollback"):
        raise RuntimeError("Unsupported web release action")
    settings = _settings(profile)
    root = settings["root"]
    live = settings["site_path"]
    with _release_lock(root), _site_lock(live), _interrupt_guard():
        state_path = root / "web-state.json"
        original_state = state_path.read_bytes() if state_path.is_file() else None
        state = json.loads(original_state) if original_state is not None else None
        if state is not None and (
            not isinstance(state, dict)
            or (state.get("schema_version"), state.get("project"), state.get("environment"))
            != (1, settings["project"], settings["environment"])
        ):
            raise RuntimeError("Saved website release identity does not match the profile")
        original = _snapshot(live)
        previous = {"site": original["site"], "release_id": None}
        if action == "rollback":
            if state is None:
                raise RuntimeError("No previous release state for rollback")
            if not state.get("previous", {}).get("site"):
                raise RuntimeError("No previous website release for rollback")
            if state["current"]["site"] != original["site"]:
                raise RuntimeError("Current website differs from saved release state")
            target = Path(state["previous"]["site"])
            current = state["previous"]
            previous = state["current"]
        else:
            if bundle is None or not isinstance(manifest, dict):
                raise RuntimeError("Publish requires a bundle and release manifest")
            for field in ("project", "environment"):
                if field in manifest and manifest[field] != settings[field]:
                    raise RuntimeError(f"Manifest {field} does not match the release profile")
            release_id = manifest.get("release_id", "")
            if not isinstance(release_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id
            ):
                raise RuntimeError("Invalid release identifier")
            source = Path(bundle) / "site"
            _validate_site(source, settings["expected_api_base"])
            release = root / "releases" / release_id
            _public_directory(release.parent)
            release.mkdir(mode=0o755)
            release.chmod(0o755)
            target = release / "site"
            shutil.copytree(source, target)
            for item in (target, *target.rglob("*")):
                item.chmod(0o755 if item.is_dir() else 0o644)
            current = {"site": str(target.resolve()), "release_id": release_id}
            if state is not None and state.get("current", {}).get("site") == original["site"]:
                previous = state["current"]
        entry = _validate_site(target, settings["expected_api_base"])
        _run(["nginx", "-t"])
        _ready(settings["health_url"])
        backup = None
        changed = False
        state_changing = False
        _public_directory(live.parent, owned=False)
        try:
            if original["kind"] == "directory":
                backup = root / "backups" / uuid.uuid4().hex
                _public_directory(backup.parent)
                changed = True
                os.replace(live, backup)
                previous = {"site": str(backup.resolve()), "release_id": None}
            changed = True
            _atomic_link(live, target)
            _verify_live(target, settings, entry)
            state_changing = True
            _write_state(
                state_path,
                {
                    "schema_version": 1,
                    "project": settings["project"],
                    "environment": settings["environment"],
                    "current": current,
                    "previous": previous,
                },
            )
        except BaseException:
            # Interruption and post-switch validation failures must both restore the prior site
            if changed:
                _restore(live, original, backup)
            if state_changing:
                if original_state is None:
                    if state_path.is_file():
                        state_path.unlink()
                elif not state_path.is_file() or state_path.read_bytes() != original_state:
                    _write_bytes(state_path, original_state)
            raise
        return {
            "status": "ok",
            "action": action,
            "project": settings["project"],
            "environment": settings["environment"],
            "release_id": current["release_id"],
            "site_path": str(live),
            "current": current,
            "previous": previous,
        }
