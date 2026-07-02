"""Tests for skills/nightly-order-sync/scripts/stamp-cursor.py."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/nightly-order-sync/scripts/stamp-cursor.py"


@pytest.fixture
def stamp_module():
    spec = importlib.util.spec_from_file_location(
        "stamp_cursor_nightly_order_sync_under_test", REPO_ROOT / SCRIPT_REL
    )
    assert spec is not None and spec.loader is not None, f"cannot load {SCRIPT_REL}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stamp_writes_supported_schema_and_utc_iso(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    payload = stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 14, 7, tzinfo=timezone.utc))
    assert payload["status"] == "stamped"
    assert payload["last_run"] == "2026-05-02T03:14:07Z"
    on_disk = json.loads(cursor.read_text())
    assert on_disk["schema_version"] == stamp_module.SUPPORTED_SCHEMA


def test_stamp_creates_parent_dirs(stamp_module, tmp_path):
    cursor = tmp_path / "nested" / "cursor.json"
    stamp_module.stamp(cursor, datetime(2026, 5, 2, tzinfo=timezone.utc))
    assert cursor.exists()


def test_stamp_overwrites_existing_cursor(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-23T01:00:00Z"}))
    stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc))
    assert json.loads(cursor.read_text())["last_run"] == "2026-05-02T03:00:00Z"


def test_stamp_leaves_no_tempfile_debris(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    stamp_module.stamp(cursor, datetime(2026, 5, 2, tzinfo=timezone.utc))
    assert list(tmp_path.glob(".cursor.json.*.tmp")) == []


def test_stamp_preserves_existing_file_mode(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-23T00:00:00Z"}))
    os.chmod(cursor, 0o600)
    stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc))
    assert stat.S_IMODE(os.stat(cursor).st_mode) == 0o600


def test_main_emits_status_stamped_and_exits_zero(tmp_path):
    cursor = tmp_path / "cursor.json"
    env = {**os.environ, "NIGHTLY_ORDER_SYNC_CURSOR": str(cursor)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "stamped"


def test_main_cli_flag_overrides_env_var(tmp_path):
    env_cursor = tmp_path / "from-env.json"
    flag_cursor = tmp_path / "from-flag.json"
    env = {**os.environ, "NIGHTLY_ORDER_SYNC_CURSOR": str(env_cursor)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL), "--cursor", str(flag_cursor)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    assert flag_cursor.exists()
    assert not env_cursor.exists()


def test_main_exits_2_on_write_failure(tmp_path):
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    os.chmod(locked_dir, 0o500)
    try:
        proc = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / SCRIPT_REL),
                "--cursor",
                str(locked_dir / "cursor.json"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 2
    finally:
        os.chmod(locked_dir, 0o700)
