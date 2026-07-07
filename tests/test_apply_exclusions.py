"""Tests for skills/check-orders/scripts/apply-exclusions.py.

Locks down the Step 6 contract:

  - EXCLUSIONS matching: source equality AND (any parsed recipient
    equals a rule address case-insensitively OR description contains a
    rule substring case-insensitively)
  - to_address parsing handles display-name wrapping and
    comma-separated multi-recipient headers (email.utils.getaddresses)
  - NULL/empty to_address falls through to the description check
  - matches are unflagged in one pass; already-unflagged matches still
    appear in excluded_ids but do not count as unflagged
  - stdout: {"excluded_ids": [...], "excluded_ids_csv": "...",
             "matched": <int>, "unflagged": <int>}
  - exit codes: 0 success, 1 IO/schema error
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


def _flag_state(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT flagged, flag_reason FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    finally:
        conn.close()


def _run(module, capsys):
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_bare_address_match_unflags(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="a1",
        email_message_id="m-a1",
        to_address="amir@sadogursky.com",
        flagged=1,
        flag_reason="Order cancelled",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["excluded_ids"] == ["a1"]
    assert payload["excluded_ids_csv"] == "a1"
    assert payload["matched"] == 1
    assert payload["unflagged"] == 1
    assert _flag_state(db_path, "a1") == (0, None)


def test_display_name_wrapping_and_case_insensitive(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="a2",
        email_message_id="m-a2",
        to_address='"Amir Sadogursky" <AMIR@SADOGURSKY.COM>',
        flagged=1,
        flag_reason="Large purchase: $300.00",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)["excluded_ids"] == ["a2"]
    assert _flag_state(db_path, "a2") == (0, None)


def test_multi_recipient_header_matches_any(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="a3",
        email_message_id="m-a3",
        to_address="baruch@sadogursky.com, amir@sadogursky.com",
        flagged=1,
        flag_reason="Overdue delivery",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)["excluded_ids"] == ["a3"]


def test_null_to_address_uses_description_fallback(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="a4",
        email_message_id="m-a4",
        to_address=None,
        description="Echo Dot (Amir)",
        flagged=1,
        flag_reason="Order cancelled",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)["excluded_ids"] == ["a4"]
    assert _flag_state(db_path, "a4") == (0, None)


def test_non_matching_source_is_not_excluded(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="n1",
        email_message_id="m-n1",
        source="shopify",
        to_address="amir@sadogursky.com",
        flagged=1,
        flag_reason="Order cancelled",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)["excluded_ids"] == []
    assert _flag_state(db_path, "n1") == (1, "Order cancelled")


def test_different_address_same_domain_is_not_excluded(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="n2",
        email_message_id="m-n2",
        to_address="notamir@sadogursky.com",
        description="Kitchen scale",
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out)["excluded_ids"] == []


def test_already_unflagged_match_counts_matched_not_unflagged(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(
        db_path,
        id="a5",
        email_message_id="m-a5",
        to_address="amir@sadogursky.com",
        flagged=0,
        flag_reason=None,
    )
    code, out, _err = _run(module, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["excluded_ids"] == ["a5"]
    assert payload["matched"] == 1
    assert payload["unflagged"] == 0


def test_csv_joins_multiple_ids_in_row_order(apply_exclusions, capsys):
    module, db_path = apply_exclusions
    _insert(db_path, id="c1", email_message_id="m-c1", to_address="amir@sadogursky.com")
    _insert(db_path, id="c2", email_message_id="m-c2", description="For Amir")
    code, out, _err = _run(module, capsys)
    assert code == 0
    payload = json.loads(out)
    assert set(payload["excluded_ids"]) == {"c1", "c2"}
    assert payload["excluded_ids_csv"] == ",".join(payload["excluded_ids"])


def test_empty_table_emits_empty_result(apply_exclusions, capsys):
    module, _db_path = apply_exclusions
    code, out, _err = _run(module, capsys)
    assert code == 0
    assert json.loads(out) == {
        "excluded_ids": [],
        "excluded_ids_csv": "",
        "matched": 0,
        "unflagged": 0,
    }
