"""Tests for skills/check-orders/scripts/ack-orders.py.

Locks down the owner-ack tool's contract (`jbaruch/nanoclaw-orders#61`):

  - stdin: ids one per line (whitespace tolerated, blank lines ignored)
  - effect: eligible (ordered/shipped) rows → status='assumed_delivered',
    flagged=0, flag_reason=NULL, last_updated=now, in one transaction
  - terminal/missing rows are left untouched and counted as not_acked
  - stdout: {"acked": <int>, "not_acked": <int>}
  - empty stdin returns {"acked": 0, "not_acked": 0}
  - exit codes: 0 success, 1 IO/schema error

`module.datetime` is frozen so the last_updated stamp is deterministic.
"""

import json
import sqlite3
from datetime import datetime, timezone

FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "amazon",
        "status": "ordered",
        "amount": 0.0,
        "currency": "USD",
        "description": "Thing",
        "order_date": "2026-04-01",
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 1,
        "flag_reason": "Ordered, not yet shipped",
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


def _row(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT status, flagged, flag_reason, last_updated FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()


def _run(module, monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", _FakeStdin(stdin_text))
    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_acks_ordered_row_to_assumed_delivered(ack_orders, monkeypatch, capsys):
    module, db_path = ack_orders
    _insert(db_path, id="ord-1", email_message_id="msg-1", status="ordered")
    code, out, _err = _run(module, monkeypatch, capsys, "ord-1\n")
    assert code == 0
    assert json.loads(out) == {"acked": 1, "not_acked": 0}
    status, flagged, flag_reason, last_updated = _row(db_path, "ord-1")
    assert status == "assumed_delivered"
    assert flagged == 0
    assert flag_reason is None
    assert last_updated == FROZEN_NOW.isoformat()


def test_acks_shipped_row(ack_orders, monkeypatch, capsys):
    module, db_path = ack_orders
    _insert(db_path, id="ord-2", email_message_id="msg-2", status="shipped")
    code, out, _err = _run(module, monkeypatch, capsys, "ord-2\n")
    assert code == 0
    assert json.loads(out) == {"acked": 1, "not_acked": 0}
    assert _row(db_path, "ord-2")[0] == "assumed_delivered"


def test_leaves_terminal_row_untouched(ack_orders, monkeypatch, capsys):
    # A cancelled order is not "delivered" — acking must not rewrite it.
    module, db_path = ack_orders
    _insert(db_path, id="ord-3", email_message_id="msg-3", status="cancelled", flagged=1)
    code, out, _err = _run(module, monkeypatch, capsys, "ord-3\n")
    assert code == 0
    assert json.loads(out) == {"acked": 0, "not_acked": 1}
    assert _row(db_path, "ord-3")[0] == "cancelled"


def test_missing_id_counts_as_not_acked(ack_orders, monkeypatch, capsys):
    module, _db_path = ack_orders
    code, out, _err = _run(module, monkeypatch, capsys, "nope\n")
    assert code == 0
    assert json.loads(out) == {"acked": 0, "not_acked": 1}


def test_already_acked_is_not_re_acked(ack_orders, monkeypatch, capsys):
    # Idempotent-by-status: a row already assumed_delivered is terminal,
    # so a repeat ack is a no-op (not_acked), not a double count.
    module, db_path = ack_orders
    _insert(db_path, id="ord-4", email_message_id="msg-4", status="assumed_delivered", flagged=0)
    code, out, _err = _run(module, monkeypatch, capsys, "ord-4\n")
    assert code == 0
    assert json.loads(out) == {"acked": 0, "not_acked": 1}


def test_mixed_batch_counts_each(ack_orders, monkeypatch, capsys):
    module, db_path = ack_orders
    _insert(db_path, id="live", email_message_id="m-live", status="ordered")
    _insert(db_path, id="dead", email_message_id="m-dead", status="refunded")
    code, out, _err = _run(module, monkeypatch, capsys, "live\ndead\nghost\n")
    assert code == 0
    assert json.loads(out) == {"acked": 1, "not_acked": 2}
    assert _row(db_path, "live")[0] == "assumed_delivered"
    assert _row(db_path, "dead")[0] == "refunded"


def test_whitespace_and_blank_lines_tolerated(ack_orders, monkeypatch, capsys):
    module, db_path = ack_orders
    _insert(db_path, id="ord-5", email_message_id="msg-5", status="ordered")
    code, out, _err = _run(module, monkeypatch, capsys, "\n  ord-5  \n\n")
    assert code == 0
    assert json.loads(out) == {"acked": 1, "not_acked": 0}


def test_empty_stdin_is_noop(ack_orders, monkeypatch, capsys):
    module, _db_path = ack_orders
    code, out, _err = _run(module, monkeypatch, capsys, "")
    assert code == 0
    assert json.loads(out) == {"acked": 0, "not_acked": 0}


class _RaisingStdin:
    def read(self):
        raise OSError("stdin gone")


def test_stdin_read_failure_exits_one_with_diagnostic(ack_orders, monkeypatch, capsys):
    module, _db_path = ack_orders
    monkeypatch.setattr("sys.stdin", _RaisingStdin())
    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    code = module.main()
    err = capsys.readouterr().err
    assert code == 1
    assert "failed to read order ids from stdin" in err


def test_missing_table_exits_one(ack_orders, monkeypatch, capsys):
    module, db_path = ack_orders
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE orders")
        conn.commit()
    finally:
        conn.close()
    code, _out, err = _run(module, monkeypatch, capsys, "ord-1\n")
    assert code == 1
    assert "ack-orders: SQLite error" in err
