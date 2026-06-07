"""Tests for skills/check-orders/scripts/get-flagged-orders.py.

Mirrors the morning-brief fetch-flagged-orders test contract — same
output shape, same ordering rule.
"""

import json
import sqlite3


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
        "flagged": 0,
        "flag_reason": None,
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


def _run(module, capsys):
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_empty_table_returns_empty_array(get_flagged_orders, capsys):
    module, _db_path = get_flagged_orders
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out) == []


def test_only_flagged_rows_appear(get_flagged_orders, capsys):
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="f1",
        email_message_id="m-f1",
        flagged=1,
        flag_reason="Order cancelled",
        description="Headphones",
        source="amazon",
        order_date="2026-04-15",
    )
    _insert(db_path, id="u1", email_message_id="m-u1", flagged=0)
    code, out, _err = _run(module, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0] == {
        "description": "Headphones",
        "flag_reason": "Order cancelled",
        "source": "amazon",
        "order_date": "2026-04-15",
    }


def test_rows_ordered_by_order_date_desc(get_flagged_orders, capsys):
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="old",
        email_message_id="m-old",
        flagged=1,
        flag_reason="x",
        description="Old",
        order_date="2026-03-01",
    )
    _insert(
        db_path,
        id="new",
        email_message_id="m-new",
        flagged=1,
        flag_reason="x",
        description="New",
        order_date="2026-04-25",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    descriptions = [r["description"] for r in json.loads(out)]
    assert descriptions == ["New", "Old"]


def test_returns_only_advertised_columns(get_flagged_orders, capsys):
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="x",
        email_message_id="m-x",
        flagged=1,
        flag_reason="Large purchase: $499.00",
        amount=499.0,
        status="shipped",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "description",
        "flag_reason",
        "source",
        "order_date",
    }
