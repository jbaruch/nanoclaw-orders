"""Tests for skills/check-orders/scripts/apply-order.py.

Locks down the script's contract:

  - stdin: single JSON object with the parsed-email fields plus computed `id`
  - stdout (success): single JSON `{"action": "inserted" | "status_updated" | "noop", "id": "..."}`
  - exit codes: 0 success, 1 schema/IO failure, 2 usage error
  - SQL: parameter-bound INSERT ... ON CONFLICT(email_message_id) DO UPDATE
    SET status, last_updated WHERE orders.status != excluded.status
"""

import json
import sqlite3


def _run(module, monkeypatch, capsys, payload, stdin_raw=None):
    """Invoke main() with the given JSON payload (or raw stdin string)."""
    text = stdin_raw if stdin_raw is not None else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", _FakeStdin(text))
    monkeypatch.setattr("sys.argv", ["apply-order.py"])
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _select_one(db_path, email_message_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT id, source, status, description, last_updated FROM orders "
            "WHERE email_message_id = ?",
            (email_message_id,),
        ).fetchone()
    finally:
        conn.close()


def _base_order():
    return {
        "id": "amazon-2026-04-01-aaaaaaaa",
        "source": "amazon",
        "status": "shipped",
        "amount": 19.99,
        "currency": "USD",
        "description": "Widget with apostrophe's in name",
        "order_date": "2026-04-01",
        "expected_delivery": "2026-04-05",
        "email_message_id": "msg-aaa",
        "to_address": "user@example.com",
    }


def test_inserts_a_fresh_order(apply_order, monkeypatch, capsys):
    module, db_path = apply_order
    code, out, err = _run(module, monkeypatch, capsys, _base_order())
    assert code == 0
    payload = json.loads(out)
    assert payload == {"action": "inserted", "id": "amazon-2026-04-01-aaaaaaaa"}
    row = _select_one(db_path, "msg-aaa")
    assert row is not None
    assert row[1:4] == ("amazon", "shipped", "Widget with apostrophe's in name")


def test_apostrophes_in_description_are_safely_bound(apply_order, monkeypatch, capsys):
    """The script's reason for existing — sanitized email content can
    contain `'`, and shell-string interpolation of those into SQL would
    blow up the statement. Parameter binding must absorb them."""
    module, db_path = apply_order
    payload = _base_order()
    payload["description"] = "It's o'clock; she's home"
    code, out, _err = _run(module, monkeypatch, capsys, payload)
    assert code == 0
    assert json.loads(out)["action"] == "inserted"
    row = _select_one(db_path, "msg-aaa")
    assert row[3] == "It's o'clock; she's home"


def test_status_update_on_conflict_with_changed_status(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    first = _base_order()
    _run(module, monkeypatch, capsys, first)
    second = dict(first)
    second["status"] = "delivered"
    code, out, _err = _run(module, monkeypatch, capsys, second)
    assert code == 0
    assert json.loads(out)["action"] == "status_updated"


def test_noop_on_conflict_with_unchanged_status(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    payload = _base_order()
    _run(module, monkeypatch, capsys, payload)
    code, out, _err = _run(module, monkeypatch, capsys, payload)
    assert code == 0
    assert json.loads(out)["action"] == "noop"


def test_empty_stdin_is_usage_error(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    code, _out, err = _run(module, monkeypatch, capsys, None, stdin_raw="")
    assert code == 2
    assert "no JSON on stdin" in err


def test_invalid_json_is_usage_error(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    code, _out, err = _run(module, monkeypatch, capsys, None, stdin_raw="{ not json")
    assert code == 2
    assert "invalid JSON" in err


def test_missing_required_field_is_usage_error(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    payload = _base_order()
    del payload["email_message_id"]
    code, _out, err = _run(module, monkeypatch, capsys, payload)
    assert code == 2
    assert "missing required field" in err
    assert "email_message_id" in err


def test_non_object_stdin_is_usage_error(apply_order, monkeypatch, capsys):
    module, _db_path = apply_order
    code, _out, err = _run(module, monkeypatch, capsys, None, stdin_raw='["list", "instead"]')
    assert code == 2
    assert "must be a JSON object" in err
