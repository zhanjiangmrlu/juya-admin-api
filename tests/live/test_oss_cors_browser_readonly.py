"""Explicit opt-in read-only actual Chromium check with signatures never logged."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.getenv("JUYA_RUN_LIVE_OSS_BROWSER_TESTS") != "true",
    reason="read-only existing-fixture browser CORS test is opt-in",
)
def test_actual_origins_read_existing_fixture_without_new_upload():
    repository = Path(__file__).resolve().parents[2]
    workspace = repository.parent
    helper = repository / "scripts/oss-cors-browser-check.py"
    assert helper.is_file(), "repository browser helper must be included"
    result = subprocess.run(
        [sys.executable, str(helper)], cwd=workspace, capture_output=True, timeout=90
    )
    if result.returncode == 77:
        pytest.skip(json.loads(result.stdout)["skipped"])
    assert result.returncode == 0, "read-only browser runner failed; raw output suppressed"
    report = json.loads(result.stdout)
    assert report["read_only"] is True and report["synthetic_existing_fixture"] is True
    expected_origins = {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:18173",
    }
    assert {row["actual_origin"] for row in report["results"]} == expected_origins
    for row in report["results"]:
        assert row["get"].get("status") == 200, f"CORS GET blocked: {row['actual_origin']}"
        assert row["get"]["hash_match"] is True
        assert row["head"].get("status") == 200, f"CORS HEAD blocked: {row['actual_origin']}"
