"""Tests for skills/check-orders/scripts/list-stuck-candidates.py.

The script does the deterministic half of stuck-order detection (`#55`):
select the `ordered` rows inside the age window as candidates and every
shipment row, leaving the free-text order-number pairing to the agent.

Tests freeze `module.date` to a fixed-today subclass (same pattern as
test_flag_anomalies.py) so the age boundaries never move with the run date.
"""

import json
import sqlite3
from datetime import date

FROZEN_TODAY = date(2026, 4, 30)

# Fixed literals relative to FROZEN_TODAY (2026-04-30):
FIVE_DAYS_AGO = "2026-04-25"  # inside min window (< 7d) — too fresh
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


def _run(module, monkeypatch, capsys):
    monkeypatch.setattr(module, "date", _FrozenDate)
    code = module.main()
    out = capsys.readouterr()
    return code, json.loads(out.out)


def test_aged_ordered_row_is_a_candidate(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(
        db_path,
        id="c1",
        email_message_id="m-c1",
        status="ordered",
        description="Stoiq Carry-On 17L",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload["candidates"] == [
        {"id": "c1", "source": "amazon", "description": "Stoiq Carry-On 17L"}
    ]
    assert payload["shipments"] == []


def test_too_fresh_ordered_row_is_not_a_candidate(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(db_path, id="fresh", email_message_id="m-fresh", order_date=FIVE_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload["candidates"] == []


def test_aged_out_ordered_row_is_not_a_candidate(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(db_path, id="old", email_message_id="m-old", order_date=HUNDRED_DAYS_AGO)
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload["candidates"] == []


def test_shipment_statuses_are_listed_as_shipments(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(db_path, id="s1", email_message_id="m-s1", status="shipped", description="on its way")
    _insert(db_path, id="s2", email_message_id="m-s2", status="delivered", description="delivered")
    _insert(
        db_path,
        id="s3",
        email_message_id="m-s3",
        status="assumed_delivered",
        description="assumed",
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload["candidates"] == []
    assert {s["id"] for s in payload["shipments"]} == {"s1", "s2", "s3"}


def test_malformed_order_date_is_not_a_candidate(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(db_path, id="bad", email_message_id="m-bad", order_date="March")
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload["candidates"] == []


def test_terminal_and_unknown_statuses_are_ignored(list_stuck_candidates, monkeypatch, capsys):
    module, db_path = list_stuck_candidates
    _insert(db_path, id="cx", email_message_id="m-cx", status="cancelled")
    _insert(db_path, id="ux", email_message_id="m-ux", status="unknown")
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload["candidates"] == []
    assert payload["shipments"] == []
