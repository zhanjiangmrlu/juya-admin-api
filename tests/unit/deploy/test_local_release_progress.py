"""Verify elapsed time and progress against the real release orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.deploy.local_release_testkit import (
    PROJECTS,
    initialize_git,
    make_workspace,
    release_module,
    write_config,
)
from tests.unit.deploy.test_local_release_cli import install_release_boundaries


def timed_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_stage=None):
    """Replace external work with known durations, retaining real CLI and logs."""
    publish = release_module("publish")
    make_workspace(tmp_path)
    initialize_git(tmp_path)
    config_path = write_config(tmp_path)
    install_release_boundaries(publish, monkeypatch, tmp_path, fail_stage=fail_stage)
    clock = {"now": 100.0}
    monkeypatch.setattr(publish.time, "monotonic", lambda: clock["now"])

    for name, seconds in (
        ("doctor", 5),
        ("run_checks", 40),
        ("build_bundle", 60),
        ("upload_and_deploy", 70),
        ("public_health", 10),
    ):
        original = getattr(publish, name)

        def timed(*args, _original=original, _seconds=seconds, **kwargs):
            try:
                return _original(*args, **kwargs)
            finally:
                clock["now"] += _seconds

        monkeypatch.setattr(publish, name, timed)

    args = [
        "--config",
        str(config_path),
        "--workspace",
        str(tmp_path),
        "--project",
        "all",
        "--environment",
        "test",
    ]
    return publish, clock, args


def read_events(tmp_path: Path):
    logs = list((tmp_path / ".local-release/runs").glob("*/release.log"))
    assert len(logs) == 1
    return [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize(
    "selection,total,label", [("juya-admin-api", 185, "3 分 5 秒"), ("all", 725, "12 分 5 秒")]
)
def test_publish_total_includes_checks_build_upload_and_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    selection,
    total,
    label,
):
    publish, _, args = timed_release(tmp_path, monkeypatch)
    args[args.index("--project") + 1] = selection
    assert publish.main(args) == 0
    output = capsys.readouterr().out
    assert "总进度" in output and "100%" in output
    assert "部署完成" in output and "总耗时: " + label in output
    assert "\x1b[" not in output  # redirected output must remain readable
    events = read_events(tmp_path)
    released = [event for event in events if event["stage"] == "released"]
    assert len(released) == (len(PROJECTS) if selection == "all" else 1)
    assert all(event["elapsed_seconds"] == 80 for event in released)
    assert events[-1]["stage"] == "publish_completed"
    assert events[-1]["elapsed_seconds"] == total


@pytest.mark.parametrize("fail_stage", ["check", "build", "upload"])
def test_failed_release_reports_elapsed_without_full_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    fail_stage,
):
    publish, clock, args = timed_release(tmp_path, monkeypatch, fail_stage=fail_stage)
    assert publish.main(args) == 1
    output = capsys.readouterr().out
    assert "部署失败" in output and "总耗时: " in output
    assert "100%" not in output and "部署完成" not in output
    events = read_events(tmp_path)
    assert events[-1]["stage"] == "publish_failed"
    assert events[-1]["elapsed_seconds"] == clock["now"] - 100


@pytest.mark.parametrize("failure,exit_code", [(TimeoutError, 1), (KeyboardInterrupt, 130)])
def test_health_failure_never_finishes_progress_and_includes_restore_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    failure,
    exit_code,
):
    publish, clock, args = timed_release(tmp_path, monkeypatch)

    def fail_health(profile):
        clock["now"] += 10
        raise failure("fixture health failure")

    monkeypatch.setattr(publish, "public_health", fail_health)
    assert publish.main(args) == exit_code
    output = capsys.readouterr().out
    assert "100%" not in output and "部署完成" not in output
    assert "总耗时: 9 分 15 秒" in output
    events = read_events(tmp_path)
    assert events[-2]["stage"] == "health_failed_rolled_back"
    assert events[-1]["elapsed_seconds"] == 555  # includes 70 seconds restoring
    assert not any(event["stage"] == "released" for event in events)


def test_rollback_reports_its_own_total_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    publish, _, args = timed_release(tmp_path, monkeypatch)
    args.extend(["--action", "rollback"])
    assert publish.main(args) == 0
    output = capsys.readouterr().out
    assert "回滚完成" in output and "100%" in output
    assert "总耗时: 5 分 25 秒" in output and "部署完成" not in output
    assert read_events(tmp_path)[-1]["elapsed_seconds"] == 325


def test_terminal_colors_make_progress_and_result_prominent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    publish, _, args = timed_release(tmp_path, monkeypatch)
    monkeypatch.setattr(publish, "terminal_supports_color", lambda: True, raising=False)
    assert publish.main(args) == 0
    output = capsys.readouterr().out
    assert "\x1b[1;36m" in output  # bold cyan progress
    assert "\x1b[1;32m" in output  # bold green success
    assert "=" * 64 in output


def test_build_only_progress_does_not_claim_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    publish, _, args = timed_release(tmp_path, monkeypatch)
    args.append("--build-only")
    assert publish.main(args) == 0
    output = capsys.readouterr().out
    assert "100%" in output and "未部署" in output
    assert "总耗时: 6 分 40 秒" in output and "部署完成" not in output
    assert read_events(tmp_path)[-1]["stage"] == "build_completed"
