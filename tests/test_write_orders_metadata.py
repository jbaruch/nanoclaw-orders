"""Tests for skills/check-orders/scripts/write-orders-metadata.py.

Locks down the script's contract:

  - With no args: stamps last_checked + last_updated with current UTC ISO
  - With one ISO arg: stamps both keys with that exact literal
  - Both writes happen in one transaction (last_checked and last_updated
    always agree on the value the script chose)
  - Stdout: `{"last_checked": "<iso>", "last_updated": "<iso>"}`
  - Exit codes: 0 success, 1 IO error, 2 invalid ISO arg
"""

import json
import sqlite3


def _run(module, monkeypatch, capsys, *args):
    monkeypatch.setattr("sys.argv", ["write-orders-metadata.py", *args])
    code = 0
    try:
        ret = module.main()
        if ret is not None:
            code = ret
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    out = capsys.readouterr()
    return code, out.out, out.err


def _read(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT key, value FROM orders_metadata ORDER BY key").fetchall()
        return dict(rows)
    finally:
        conn.close()


def test_no_arg_stamps_current_iso(write_orders_metadata, monkeypatch, capsys):
    module, db_path = write_orders_metadata
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert set(payload.keys()) == {"last_checked", "last_updated"}
    assert payload["last_checked"] == payload["last_updated"]
    # Shape sanity — ISO-8601 prefix.
    assert payload["last_checked"].startswith(f"{payload['last_checked'][:4]}-")
    stored = _read(db_path)
    assert stored["last_checked"] == payload["last_checked"]
    assert stored["last_updated"] == payload["last_updated"]


def test_explicit_iso_arg_is_used_verbatim(write_orders_metadata, monkeypatch, capsys):
    module, db_path = write_orders_metadata
    fixed = "2026-04-30T12:00:00Z"
    code, out, _err = _run(module, monkeypatch, capsys, fixed)
    assert code == 0
    payload = json.loads(out)
    assert payload == {"last_checked": fixed, "last_updated": fixed}
    assert _read(db_path) == {"last_checked": fixed, "last_updated": fixed}


def test_invalid_iso_arg_is_usage_error(write_orders_metadata, monkeypatch, capsys):
    module, _db_path = write_orders_metadata
    code, _out, err = _run(module, monkeypatch, capsys, "not-a-date")
    assert code == 2
    assert "invalid ISO-8601" in err


def test_too_many_args_is_usage_error(write_orders_metadata, monkeypatch, capsys):
    module, _db_path = write_orders_metadata
    code, _out, err = _run(module, monkeypatch, capsys, "2026-04-30T12:00:00Z", "extra")
    assert code == 2
    assert "Usage" in err


def test_idempotent_replay_overwrites(write_orders_metadata, monkeypatch, capsys):
    module, db_path = write_orders_metadata
    _run(module, monkeypatch, capsys, "2026-04-30T12:00:00Z")
    _run(module, monkeypatch, capsys, "2026-04-30T13:00:00Z")
    assert _read(db_path) == {
        "last_checked": "2026-04-30T13:00:00Z",
        "last_updated": "2026-04-30T13:00:00Z",
    }


def _columns(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(orders_metadata)")]
    finally:
        conn.close()


def _read_schema_versions(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return dict(
            conn.execute("SELECT key, schema_version FROM orders_metadata ORDER BY key").fetchall()
        )
    finally:
        conn.close()


def test_first_write_adds_schema_version_column_and_stamps_v1(
    write_orders_metadata, monkeypatch, capsys
):
    """The owner-skill migration per stateful-artifacts.md: the
    orchestrator's state-001 migration shipped `orders_metadata` without
    a `schema_version` column. The writer adds it idempotently on
    first invocation (via `_ensure_schema_version_column`) and stamps
    every UPSERT with `SCHEMA_VERSION=1`. Asserting both the column
    appears AND the rows carry the expected version."""
    module, db_path = write_orders_metadata
    assert "schema_version" not in _columns(
        db_path
    ), "fixture should mirror the pre-migration table shape — bare key/value"

    code, _out, _err = _run(module, monkeypatch, capsys, "2026-04-30T12:00:00Z")
    assert code == 0

    assert "schema_version" in _columns(db_path)
    assert _read_schema_versions(db_path) == {
        "last_checked": 1,
        "last_updated": 1,
    }


def test_second_write_does_not_re_alter_table(write_orders_metadata, monkeypatch, capsys):
    """Idempotency: the migration helper is a no-op when the column
    already exists. SQLite would raise `duplicate column name` if the
    ALTER ran a second time — exercising two back-to-back writes pins
    the idempotency invariant."""
    module, db_path = write_orders_metadata
    code1, _, _ = _run(module, monkeypatch, capsys, "2026-04-30T12:00:00Z")
    code2, _, _ = _run(module, monkeypatch, capsys, "2026-04-30T13:00:00Z")
    assert code1 == 0 and code2 == 0
    assert _read_schema_versions(db_path) == {
        "last_checked": 1,
        "last_updated": 1,
    }
