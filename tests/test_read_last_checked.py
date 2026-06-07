"""Tests for skills/check-orders/scripts/read-last-checked.py."""

import json
import sqlite3


def _set_metadata(db_path, key, value):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO orders_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _run(module, capsys):
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_returns_null_when_marker_absent(read_last_checked, capsys):
    module, _db_path = read_last_checked
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out) == {"last_checked": None}


def test_returns_iso_when_marker_present(read_last_checked, capsys):
    module, db_path = read_last_checked
    _set_metadata(db_path, "last_checked", "2026-04-30T12:00:00Z")
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out) == {"last_checked": "2026-04-30T12:00:00Z"}


def test_other_metadata_keys_dont_leak(read_last_checked, capsys):
    """The script targets `key = 'last_checked'` specifically — if a
    `last_updated` row exists, it must not surface here."""
    module, db_path = read_last_checked
    _set_metadata(db_path, "last_updated", "2026-04-30T13:00:00Z")
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out) == {"last_checked": None}
