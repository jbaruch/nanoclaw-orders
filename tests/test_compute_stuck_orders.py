"""Tests for skills/check-orders/scripts/compute-stuck-orders.py.

The script deterministically computes the ids of orders stuck in `ordered`
with no shipment for the same `(source, order_number)` logical key (`#55`).
Pairing is a join on the persisted `order_number` column, so no agent step
is involved.

Tests freeze `module.date` to a fixed-today subclass so the age boundaries
never move with the run date.
"""

import json
import sqlite3
from datetime import date

FROZEN_TODAY = date(2026, 4, 30)

# Fixed literals relative to FROZEN_TODAY (2026-04-30):
FIVE_DAYS_AGO = "2026-04-25"  # below min window (< 7d) — too fresh
SEVEN_DAYS_AGO = "2026-04-23"  # exactly at the inclusive min boundary
FORTY_FIVE_DAYS_AGO = "2026-03-16"  # in window
HUNDRED_DAYS_AGO = "2026-01-20"  # past max window (> 90d) — aged out


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return FROZEN_TODAY


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "amazon",
        "status": "ordered",
        "amount": 0.0,
        "currency": "USD",
        "description": "Thing",
        "order_date": FORTY_FIVE_DAYS_AGO,
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 0,
        "flag_reason": None,
        "last_updated": "2026-04-01T00:00:00Z",
        "order_number": None,
    }
    defaults.update(fields)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO orders (id, source, status, amount, currency, description, "
            "order_date, expected_delivery, email_message_id, to_address, flagged, "
            "flag_reason, last_updated, order_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(defaults.values()),
        )
        conn.commit()
    finally:
        conn.close()


def _run(module, monkeypatch, capsys):
    monkeypatch.setattr(module, "date", _FrozenDate)
    code = module.main()
    out = capsys.readouterr()
    return code, json.loads(out.out)


def test_aged_ordered_without_order_number_is_stuck(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="stoiq",
        email_message_id="m-stoiq",
        description="Stoiq Carry-On 17L",
        order_number=None,
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": ["stoiq"]}


def test_too_fresh_is_not_stuck(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(db_path, id="fresh", email_message_id="m-fresh", order_date=FIVE_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


def test_exactly_min_age_is_stuck(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(db_path, id="edge", email_message_id="m-edge", order_date=SEVEN_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["edge"]}


def test_aged_out_is_not_stuck(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(db_path, id="old", email_message_id="m-old", order_date=HUNDRED_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


def test_ordered_with_shipped_sibling_is_not_stuck(compute_stuck_orders, monkeypatch, capsys):
    # The confirmation and shipment rows of one order share (source,
    # order_number); the aged `ordered` row is not stuck because a shipment
    # row exists for the same logical order.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="pair-ord",
        email_message_id="m-pair-ord",
        status="ordered",
        source="amazon",
        order_number="W1584689498",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _insert(
        db_path,
        id="pair-ship",
        email_message_id="m-pair-ship",
        status="shipped",
        source="amazon",
        order_number="W1584689498",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


def test_ordered_with_order_number_but_no_sibling_is_stuck(
    compute_stuck_orders, monkeypatch, capsys
):
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="lone",
        email_message_id="m-lone",
        source="other",
        order_number="140898",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["lone"]}


def test_shipment_of_a_different_source_does_not_pair(compute_stuck_orders, monkeypatch, capsys):
    # Same order_number, different source → different logical order → the
    # ordered row is still stuck.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="ord-a",
        email_message_id="m-ord-a",
        status="ordered",
        source="amazon",
        order_number="12345",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _insert(
        db_path,
        id="ship-b",
        email_message_id="m-ship-b",
        status="shipped",
        source="shopify",
        order_number="12345",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["ord-a"]}


def test_stuck_ids_sorted_ascending(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(db_path, id="zeta", email_message_id="m-z", order_date=FORTY_FIVE_DAYS_AGO)
    _insert(db_path, id="alpha", email_message_id="m-a", order_date=FORTY_FIVE_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["alpha", "zeta"]}
