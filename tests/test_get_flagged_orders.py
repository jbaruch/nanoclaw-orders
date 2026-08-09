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
        "merchant": None,
        "order_number": None,
    }
    defaults.update(fields)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO orders (id, source, status, amount, currency, description, "
            "order_date, expected_delivery, email_message_id, to_address, flagged, "
            "flag_reason, last_updated, merchant, order_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        "merchant": None,
    }


def test_surfaces_captured_merchant(get_flagged_orders, capsys):
    # `#55`: a captured merchant is returned so the alert can identify a
    # flagged item whose source is `other`.
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="fm",
        email_message_id="m-fm",
        flagged=1,
        flag_reason="Ordered, not yet shipped",
        description="your order #140898",
        source="other",
        merchant="Ragnar",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)[0]["merchant"] == "Ragnar"


def test_collapses_rows_sharing_source_and_order_number(get_flagged_orders, capsys):
    # `#55`: the confirmation and shipment rows of one order (same source +
    # order_number) collapse to one alert line — the most recent by
    # order_date. order_number itself is not in the output shape.
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="conf",
        email_message_id="m-conf",
        flagged=1,
        flag_reason="Ordered, not yet shipped",
        description="your order W1584689498",
        source="amazon",
        order_number="W1584689498",
        order_date="2026-04-10",
    )
    _insert(
        db_path,
        id="ship",
        email_message_id="m-ship",
        flagged=1,
        flag_reason="Overdue delivery",
        description="your order W1584689498 is on the way",
        source="amazon",
        order_number="W1584689498",
        order_date="2026-04-20",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["order_date"] == "2026-04-20"  # most recent representative
    assert "order_number" not in rows[0]


def test_null_order_number_rows_are_not_collapsed(get_flagged_orders, capsys):
    # Two flagged rows with no order_number cannot be paired — both show.
    module, db_path = get_flagged_orders
    _insert(
        db_path,
        id="a",
        email_message_id="m-a",
        flagged=1,
        flag_reason="Ordered, not yet shipped",
        description="Stoiq Carry-On 17L",
        order_number=None,
    )
    _insert(
        db_path,
        id="b",
        email_message_id="m-b",
        flagged=1,
        flag_reason="Ordered, not yet shipped",
        description="Ragnar Armoury",
        order_number=None,
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert len(json.loads(out)) == 2


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
        "merchant",
    }
