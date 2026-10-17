import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture
def builder():
    spec = importlib.util.spec_from_file_location("cached_build", ROOT / "deploy/cached-build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_export(path: Path, value: str = "new") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": []}))
    (path / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (path / "payload").write_text(value)


class DockerFixture:
    def __init__(self, *, fail: str = ""):
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    def __call__(self, label: str, args: list[str]):
        self.calls.append((label, args))
        if label == self.fail:
            raise RuntimeError("fixture Docker operation failed")
        exporter = args[args.index("--output") + 1]
        Path(exporter.split("dest=", 1)[1]).write_bytes(b"fixture image archive")
        if "--cache-to" in args:
            location = args[args.index("--cache-to") + 1].split("dest=", 1)[1].split(",")[0]
            cache_export(Path(location))
        return {"seconds": 0.1, "cached_steps": 2 if "--cache-from" in args else 0}


def test_warm_build_imports_previous_cache_and_replaces_it_without_accumulation(builder, tmp_path):
    context = tmp_path / "repo"
    context.mkdir()
    cache = tmp_path / "cache"
    docker = DockerFixture()
    for number in (1, 2):
        archive = tmp_path / f"run-{number}" / "image.tar"
        builder.build_cached_image(
            "juya-admin-api-test:" + "a" * 40, archive, cache, context, docker
        )
        assert archive.is_file()
    real_builds = [args for label, args in docker.calls if label == "docker-build"]
    assert "--cache-from" not in real_builds[0]
    assert f"type=local,src={cache / 'current'}" in real_builds[1]
    assert "--load" not in real_builds[1]
    assert all(args[:3] == ["docker", "buildx", "build"] for _, args in docker.calls)
    assert not list(cache.glob("next-*"))
    assert not list(cache.glob("previous-*"))
    metrics = json.loads((tmp_path / "run-2" / "build-metrics.json").read_text())
    assert metrics["cache_imported"] is True
    assert metrics["stages"]["docker-build"]["cached_steps"] == 2


@pytest.mark.parametrize("failure", ["cache-probe-export", "cache-probe-import", "docker-build"])
def test_failed_probe_or_build_preserves_previous_cache(builder, tmp_path, failure):
    context = tmp_path / "repo"
    context.mkdir()
    cache = tmp_path / "cache"
    builder.build_cached_image(
        "fixture:old", tmp_path / "old/image.tar", cache, context, DockerFixture()
    )
    (cache / "current/payload").write_text("previous good cache")
    docker = DockerFixture(fail=failure)
    with pytest.raises(RuntimeError, match="fixture Docker"):
        builder.build_cached_image(
            "fixture:new", tmp_path / "new/image.tar", cache, context, docker
        )
    assert (cache / "current/payload").read_text() == "previous good cache"
    assert not list(cache.glob("next-*"))
    if failure.startswith("cache-probe"):
        assert not any(label == "docker-build" for label, _ in docker.calls)


def test_refuses_unowned_cache_directory_without_touching_its_files(builder, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "keep.txt").write_text("unrelated")
    docker = DockerFixture()
    with pytest.raises(ValueError, match="dedicated empty directory"):
        builder.build_cached_image("fixture:new", tmp_path / "image.tar", cache, tmp_path, docker)
    assert (cache / "keep.txt").read_text() == "unrelated"
    assert not docker.calls


def test_rejects_cache_inside_build_context(builder, tmp_path):
    with pytest.raises(ValueError, match="outside the build context"):
        builder.build_cached_image(
            "fixture:new", tmp_path / "image.tar", tmp_path / "cache", tmp_path, DockerFixture()
        )


def test_incomplete_export_does_not_replace_last_good_cache(builder, tmp_path):
    context = tmp_path / "repo"
    context.mkdir()
    cache = tmp_path / "cache"
    builder.build_cached_image(
        "fixture:old", tmp_path / "old/image.tar", cache, context, DockerFixture()
    )
    (cache / "current/payload").write_text("previous good cache")
    docker = DockerFixture()

    def incomplete(label, args):
        result = docker(label, args)
        if label == "docker-build":
            location = args[args.index("--cache-to") + 1].split("dest=", 1)[1].split(",")[0]
            (Path(location) / "index.json").unlink()
        return result

    with pytest.raises(ValueError, match="cache index"):
        builder.build_cached_image(
            "fixture:new", tmp_path / "new/image.tar", cache, context, incomplete
        )
    assert (cache / "current/payload").read_text() == "previous good cache"
