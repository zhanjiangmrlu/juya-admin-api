import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def posix_path(path: Path) -> str:
    # 将本机临时路径转换成当前平台 Shell 可以访问的绝对路径。
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


@pytest.fixture
def ffmpeg_cache(tmp_path: Path):
    # 只模拟官方下载失败和预先构建的工具缓存,不运行开发机媒体程序。
    bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).exists():
        pytest.skip("bash required")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "curl").write_text('#!/bin/sh\necho download >> "$CALL_LOG"\nexit 78\n')
    if os.name == "nt":
        # Git Bash 不提供 Linux 文件锁;真实 Linux 验证仍使用系统 flock。
        (binaries / "flock").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run([bash, "-c", f"chmod +x '{posix_path(binaries)}'/*"], check=True)
    environment = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PATH": posix_path(binaries) + ":/usr/bin:/bin",
        "CALL_LOG": posix_path(tmp_path / "calls"),
    }

    def run():
        return subprocess.run(
            [
                bash,
                "-c",
                'export PATH="$1"; exec sh "$2" "$3" "$4"',
                "cache-test",
                environment["PATH"],
                posix_path(ROOT / "deploy/ci-prepare-ffmpeg.sh"),
                posix_path(tmp_path / "tools"),
                posix_path(tmp_path / "cache"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    return bash, run


def seed_cache(ffmpeg_cache, tmp_path: Path) -> Path:
    # 下载失败不能生成有效缓存;随后放入带完整校验清单的隔离工具夹具。
    result = ffmpeg_cache[1]()
    assert result.returncode == 78, result.stderr
    entry = next((tmp_path / "cache").glob("ffmpeg-*"))
    install = entry / "install"
    (install / "bin").mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        binary = install / "bin" / name
        binary.write_text(f'#!/bin/sh\necho "{name} version 7.1.5"\n', newline="\n")
    subprocess.run(
        [
            ffmpeg_cache[0],
            "-c",
            f"cd '{posix_path(install)}'; chmod +x bin/*; "
            "sha256sum bin/ffmpeg bin/ffprobe > SHA256SUMS",
        ],
        check=True,
    )
    (tmp_path / "calls").unlink()
    return install


def test_verified_ffmpeg_cache_avoids_download_and_compile(ffmpeg_cache, tmp_path: Path):
    seed_cache(ffmpeg_cache, tmp_path)
    result = ffmpeg_cache[1]()
    assert result.returncode == 0, result.stderr
    assert "Reusing verified FFmpeg 7.1.5 cache" in result.stdout
    assert not (tmp_path / "calls").exists()
    assert (tmp_path / "tools/bin/ffmpeg").is_file()


def test_corrupt_ffmpeg_cache_is_not_reused(ffmpeg_cache, tmp_path: Path):
    install = seed_cache(ffmpeg_cache, tmp_path)
    (install / "bin/ffmpeg").write_text("corrupt")
    result = ffmpeg_cache[1]()
    assert result.returncode == 78
    assert "Reusing verified" not in result.stdout
    assert (tmp_path / "calls").read_text().strip() == "download"


@pytest.mark.parametrize("omitted", ["ffmpeg", "ffprobe"])
def test_incomplete_ffmpeg_manifest_is_not_reused(ffmpeg_cache, tmp_path: Path, omitted: str):
    install = seed_cache(ffmpeg_cache, tmp_path)
    manifest = install / "SHA256SUMS"
    manifest.write_text(
        "\n".join(line for line in manifest.read_text().splitlines() if not line.endswith(omitted))
        + "\n",
        newline="\n",
    )
    result = ffmpeg_cache[1]()
    assert result.returncode == 78
    assert "Reusing verified" not in result.stdout


def test_interrupted_ffmpeg_download_never_publishes_cache(ffmpeg_cache, tmp_path: Path):
    result = ffmpeg_cache[1]()
    assert result.returncode == 78, result.stderr
    assert not list((tmp_path / "cache").glob("ffmpeg-*/install/SHA256SUMS"))
