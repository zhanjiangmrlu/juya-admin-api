#!/usr/bin/env python3
"""Export a Docker image and a bounded, reusable BuildKit cache (Python 3.6+)."""
# ruff: noqa: UP017 -- Flow's Alinux3 system Python is 3.6; datetime.UTC needs 3.11.

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

OWNER = "juya-admin-api-buildkit-cache-v1\n"
MAX_CACHE_BYTES = 1536 * 1024 * 1024


def run(label, args):
    started = time.monotonic()
    print(f"[artifact] {label} start", flush=True)
    cached = 0
    with subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        for line in process.stdout:
            print(line, end="", flush=True)
            if re.match(r"^#\d+ CACHED\s*$", line):
                cached += 1
        status = process.wait()
    seconds = round(time.monotonic() - started, 3)
    print(f"[artifact] {label} elapsed={seconds}s cached_steps={cached}", flush=True)
    if status:
        raise subprocess.CalledProcessError(status, args)
    return {"seconds": seconds, "cached_steps": cached}


@contextmanager
def cache_lock(root):
    with (root / ".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def remove_owned(root, path):
    # Only remove this helper's temporary generations, never arbitrary cache directories.
    if path.parent != root or not path.name.startswith(("next-", "previous-")):
        raise ValueError("refusing to remove an unowned cache path")
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("cache generations must not be symlinks")
    shutil.rmtree(str(path))


def valid_index(path):
    try:
        index = json.loads((path / "index.json").read_text(encoding="utf-8"))
        layout = json.loads((path / "oci-layout").read_text(encoding="utf-8"))
        if index["schemaVersion"] != 2 or not isinstance(index["manifests"], list):
            raise ValueError("invalid schema")
        if layout["imageLayoutVersion"] != "1.0.0":
            raise ValueError("invalid layout")
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise ValueError(f"missing or invalid BuildKit cache index: {path}") from error


def command(image, archive):
    return [
        "docker",
        "buildx",
        "build",
        "--progress=plain",
        "--platform",
        "linux/amd64",
        "--tag",
        image,
        "--output",
        f"type=docker,dest={archive}",
    ]


def build_cached_image(image, archive, root, context, runner=run):
    root, context, archive = Path(root).resolve(), Path(context).resolve(), Path(archive).resolve()
    if any("," in str(path) for path in (root, archive)):
        raise ValueError("cache and archive paths must not contain commas")
    if root.exists() and any(root.iterdir()) and not (root / ".owner").is_file():
        raise ValueError("use a dedicated empty directory for JUYA_DOCKER_CACHE_DIR")
    if root == context or context in root.parents:
        raise ValueError("JUYA_DOCKER_CACHE_DIR must be outside the build context")
    root.mkdir(parents=True, exist_ok=True)
    owner = root / ".owner"
    if owner.exists() and owner.read_text(encoding="utf-8") != OWNER:
        raise ValueError("cache directory belongs to another tool")
    owner.write_text(OWNER, encoding="utf-8")
    archive.parent.mkdir(parents=True, exist_ok=True)
    stages = {}
    started = datetime.now(timezone.utc).isoformat()
    current = root / "current"
    with cache_lock(root):
        if current.is_symlink():
            raise ValueError("cache generations must not be symlinks")
        previous = list(root.glob("previous-*"))
        if not current.exists() and len(previous) == 1:
            previous[0].rename(current)
        for stale in list(root.glob("next-*")) + list(root.glob("previous-*")):
            remove_owned(root, stale)
        imported = current.exists()
        if imported:
            valid_index(current)
        print(
            "[artifact] cache {} path={}".format("restore" if imported else "miss", current),
            flush=True,
        )
        # Probe the actual selected builder. Public Flow Docker proxies may support only
        # a subset of Docker APIs; no version query, child container or new builder is used.
        with tempfile.TemporaryDirectory(prefix="juya-buildkit-probe-") as directory:
            probe = Path(directory)
            probe_context = probe / "context"
            probe_context.mkdir()
            (probe_context / "Dockerfile").write_text("FROM scratch\nCOPY marker /marker\n")
            (probe_context / "marker").write_text("juya-buildkit-cache-probe\n")
            probe_cache = probe / "cache"
            args = command("juya-buildkit-probe:local", probe / "first.tar")
            args += [
                "--cache-to",
                f"type=local,dest={probe_cache},mode=max",
                str(probe_context),
            ]
            stages["cache-probe-export"] = runner("cache-probe-export", args)
            valid_index(probe_cache)
            args = command("juya-buildkit-probe:local", probe / "second.tar")
            args += ["--cache-from", f"type=local,src={probe_cache}", str(probe_context)]
            stages["cache-probe-import"] = runner("cache-probe-import", args)
        next_cache = Path(tempfile.mkdtemp(prefix="next-", dir=str(root)))
        try:
            args = command(image, archive)
            if imported:
                args += ["--cache-from", f"type=local,src={current}"]
            args += ["--cache-to", f"type=local,dest={next_cache},mode=max", str(context)]
            stages["docker-build"] = runner("docker-build", args)
            if not archive.is_file() or not archive.stat().st_size:
                raise ValueError("Docker did not export an image archive")
            valid_index(next_cache)
            size = sum(path.stat().st_size for path in next_cache.rglob("*") if path.is_file())
            exported = size <= MAX_CACHE_BYTES
            if exported:
                previous = root / ("previous-" + uuid.uuid4().hex)
                if current.exists():
                    current.rename(previous)
                next_cache.rename(current)
                if previous.exists():
                    remove_owned(root, previous)
                print(f"[artifact] cache saved bytes={size}", flush=True)
            else:
                print(
                    "[artifact] cache export exceeds 1.5 GiB; retaining previous cache", flush=True
                )
            metrics = {
                "schema_version": 1,
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "cache_imported": imported,
                "cache_exported": exported,
                "cache_bytes": size,
                "stages": stages,
            }
            (archive.parent / "build-metrics.json").write_text(
                json.dumps(metrics, indent=2) + "\n",
                encoding="utf-8",
            )
        finally:
            if next_cache.exists():
                remove_owned(root, next_cache)


def main():
    if len(sys.argv) != 4:
        print("usage: cached-build.py <image> <output-image.tar> <cache-dir>", file=sys.stderr)
        return 64
    try:
        build_cached_image(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path.cwd())
    except subprocess.CalledProcessError as error:
        print(
            "cached build failed; check Buildx local cache/export support in this Flow task",
            file=sys.stderr,
        )
        return error.returncode if error.returncode > 0 else 1
    except (OSError, ValueError) as error:
        print(f"cached build failed: {error}", file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    sys.exit(main())
