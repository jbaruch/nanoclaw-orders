"""Tests for skills/check-orders/scripts/unflag-orders.py.

Locks down the script's contract:

  - stdin: ids one per line (whitespace tolerated, blank lines ignored)
  - per-id parameter-bound UPDATE in one transaction
  - stdout: {"unflagged_existing": <int>, "missing_ids": <int>}
  - empty stdin returns {"unflagged_existing": 0, "missing_ids": 0}
  - exit codes: 0 success, 1 IO/schema error
"""

import json
import sqlite3


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "amazon",
        "status": "shipped",
        "amount": 0.0,
        "currency": "USD",
        "description": "Thing",
        "order_date": "2026-04-01",
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 1,
        "flag_reason": "Order cancelled",
        "last_updated": "2026-04-01T00:00:00Z",
    }
    defaults.update(fields)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO orders (id, source, status, amount, currency, description, "
            "order_date, expected_delivery, email_message_id, to_address, flagged, "
            "flag_reason, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(defaults.values()),
        )
        conn.commit()
    finally:
        conn.close()


def _row_state(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT flagged, flag_reason FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()


def _run(module, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", _FakeStdin(stdin_text))
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_unflags_one_existing_row(unflag_orders, monkeypatch, capsys):
    module, db_path = unflag_orders
    _insert(db_path, id="ord-1", email_message_id="m-1", flagged=1, flag_reason="Order cancelled")
    code, out, _err = _run(module, monkeypatch, capsys, "ord-1\n")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 1, "missing_ids": 0}
    assert _row_state(db_path, "ord-1") == (0, None)


def test_counts_missing_ids_separately(unflag_orders, monkeypatch, capsys):
    module, db_path = unflag_orders
    _insert(db_path, id="ord-real", email_message_id="m-real", flagged=1)
    code, out, _err = _run(module, monkeypatch, capsys, "ord-real\nord-fake\n")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 1, "missing_ids": 1}


def test_blank_lines_and_whitespace_tolerated(unflag_orders, monkeypatch, capsys):
    module, db_path = unflag_orders
    _insert(db_path, id="ord-x", email_message_id="m-x", flagged=1)
    code, out, _err = _run(module, monkeypatch, capsys, "\n  ord-x  \n\n\n")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 1, "missing_ids": 0}


def test_empty_stdin_is_clean_no_op(unflag_orders, monkeypatch, capsys):
    module, _db_path = unflag_orders
    code, out, _err = _run(module, monkeypatch, capsys, "")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 0, "missing_ids": 0}


def test_only_whitespace_stdin_is_clean_no_op(unflag_orders, monkeypatch, capsys):
    module, _db_path = unflag_orders
    code, out, _err = _run(module, monkeypatch, capsys, "   \n\t\n  \n")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 0, "missing_ids": 0}


def test_already_unflagged_row_still_counted_as_existing(unflag_orders, monkeypatch, capsys):
    """Idempotency: re-running unflag on an already-unflagged row
    should still report it as 'existing' (the row matched), not as
    'missing'. UPDATE with no actual change still has rowcount == 1
    in SQLite when the WHERE clause matches."""
    module, db_path = unflag_orders
    _insert(
        db_path,
        id="ord-already-clean",
        email_message_id="m-clean",
        flagged=0,
        flag_reason=None,
    )
    code, out, _err = _run(module, monkeypatch, capsys, "ord-already-clean\n")
    assert code == 0
    assert json.loads(out) == {"unflagged_existing": 1, "missing_ids": 0}
