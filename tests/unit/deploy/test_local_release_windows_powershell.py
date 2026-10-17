"""Verify native Windows PowerShell parsing and entrypoint failure propagation."""

import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[4]


def windows_powershell():
    """Locate the same Windows PowerShell executable used from Explorer."""
    executable = shutil.which("powershell.exe")
    if not executable:
        pytest.skip("native Windows PowerShell is required")
    return executable


@pytest.mark.parametrize(
    "relative",
    [
        "publish.ps1",
        "juya-admin-api/deploy/publish.ps1",
        "juya-admin/deploy/publish.ps1",
        "juya-miniapp/deploy/publish.ps1",
    ],
)
def test_native_windows_powershell_parses_release_entrypoints(tmp_path, relative):
    """Use the Windows parser so UTF-8 Chinese cannot silently break entrypoints."""
    probe = tmp_path / "parse-release.ps1"
    probe.write_text(
        "param([string]$Target)\n"
        "$taskTokens=$null; $taskErrors=$null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$Target,[ref]$taskTokens,[ref]$taskErrors)\n"
        "if ($taskErrors.Count) { Write-Output 'parse-failed'; exit 1 }\n"
        "Write-Output 'parse-ok'; exit 0\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            windows_powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
            str(WORKSPACE / relative),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, relative + " failed native PowerShell parsing"
    assert "parse-ok" in result.stdout


def test_workspace_entry_does_not_report_success_for_a_broken_child_script(tmp_path):
    """A real child parser error must produce a nonzero process exit code."""
    shutil.copyfile(WORKSPACE / "publish.ps1", tmp_path / "publish.ps1")
    child = tmp_path / "juya-admin-api/deploy/publish.ps1"
    child.parent.mkdir(parents=True)
    child.write_text(
        "param([string]$Project,[string]$Environment,[string]$Action)\n)\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            windows_powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "publish.ps1"),
            "-Project",
            "all",
            "-Environment",
            "test",
            "-Action",
            "plan",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0, "child parser failure was reported as a successful operation"
