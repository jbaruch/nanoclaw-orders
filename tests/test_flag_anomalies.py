"""Tests for skills/check-orders/scripts/flag-anomalies.py.

Covers each row of the Step 8 rule table:

  | status     | extra condition         | flag_reason             | cutoff |
  |------------|-------------------------|-------------------------|--------|
  | cancelled  | -                       | "Order cancelled"       | 14d    |
  | refunded   | -                       | "Refund/return"         | 14d    |
  | non-delivered amount>200                | "Large purchase: $X.YY" | none   |
  | delivered, amount>200                   | "Large purchase: $X.YY" | 14d    |
  | shipped|ordered overdue expected_delivery               | "Overdue delivery"      | 30d    |

Plus the unflag-past-cutoff branch and the EXCLUDED_IDS env-var honour.
"""

import json
import sqlite3
from datetime import date, timedelta


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "amazon",
        "status": "shipped",
        "amount": 0.0,
        "currency": "USD",
        "description": "Thing",
        "order_date": date.today().isoformat(),
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


def _row(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT flagged, flag_reason FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    finally:
        conn.close()


def _run(module, monkeypatch, capsys, excluded_ids=""):
    monkeypatch.setenv("EXCLUDED_IDS", excluded_ids)
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_flags_cancelled_within_14_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    recent = (date.today() - timedelta(days=10)).isoformat()
    _insert(db_path, id="c1", email_message_id="m-c1", status="cancelled", order_date=recent)
    code, _out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    flagged, reason = _row(db_path, "c1")
    assert flagged == 1
    assert reason == "Order cancelled"


def test_unflags_cancelled_past_14_days(flag_anomalies, monkeypatch, capsys):
    # 20 days is the regression-relevant boundary: under the old 60d
    # cutoff this row would stay flagged; under 14d it must unflag.
    module, db_path = flag_anomalies
    old = (date.today() - timedelta(days=20)).isoformat()
    _insert(
        db_path,
        id="c2",
        email_message_id="m-c2",
        status="cancelled",
        order_date=old,
        flagged=1,
        flag_reason="Order cancelled",
    )
    _run(module, monkeypatch, capsys)
    flagged, reason = _row(db_path, "c2")
    assert flagged == 0
    assert reason is None


def test_flags_refunded_within_14_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    recent = (date.today() - timedelta(days=10)).isoformat()
    _insert(db_path, id="r1", email_message_id="m-r1", status="refunded", order_date=recent)
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "r1") == (1, "Refund/return")


def test_unflags_refunded_past_14_days(flag_anomalies, monkeypatch, capsys):
    # Mirror of the cancelled past-cutoff case: 20 days would stay
    # flagged under the old 60d window and must unflag under 14d.
    module, db_path = flag_anomalies
    old = (date.today() - timedelta(days=20)).isoformat()
    _insert(
        db_path,
        id="r2",
        email_message_id="m-r2",
        status="refunded",
        order_date=old,
        flagged=1,
        flag_reason="Refund/return",
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "r2") == (0, None)


def test_flags_large_non_delivered_purchase_with_no_cutoff(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    very_old = (date.today() - timedelta(days=200)).isoformat()
    _insert(
        db_path,
        id="lp1",
        email_message_id="m-lp1",
        status="shipped",
        order_date=very_old,
        amount=499.99,
    )
    _run(module, monkeypatch, capsys)
    flagged, reason = _row(db_path, "lp1")
    assert flagged == 1
    assert reason == "Large purchase: $499.99"


def test_unflags_large_delivered_purchase_past_14_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    old = (date.today() - timedelta(days=20)).isoformat()
    _insert(
        db_path,
        id="lp2",
        email_message_id="m-lp2",
        status="delivered",
        order_date=old,
        amount=300.0,
        flagged=1,
        flag_reason="Large purchase: $300.00",
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "lp2") == (0, None)


def test_flags_overdue_shipped_within_30_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    overdue = (date.today() - timedelta(days=10)).isoformat()
    _insert(
        db_path,
        id="od1",
        email_message_id="m-od1",
        status="shipped",
        expected_delivery=overdue,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "od1") == (1, "Overdue delivery")


def test_does_not_flag_overdue_past_30_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    way_overdue = (date.today() - timedelta(days=45)).isoformat()
    _insert(
        db_path,
        id="od2",
        email_message_id="m-od2",
        status="shipped",
        expected_delivery=way_overdue,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "od2") == (0, None)


def test_excluded_ids_env_var_skips_flagging(flag_anomalies, monkeypatch, capsys):
    """Step 6 exclusions: ids passed via EXCLUDED_IDS must not be re-flagged."""
    module, db_path = flag_anomalies
    recent = (date.today() - timedelta(days=10)).isoformat()
    _insert(
        db_path,
        id="ex1",
        email_message_id="m-ex1",
        status="cancelled",
        order_date=recent,
    )
    # Without exclusion: would flag.
    _run(module, monkeypatch, capsys, excluded_ids="ex1")
    assert _row(db_path, "ex1") == (0, None)


def test_status_unknown_does_not_flag(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(db_path, id="unk", email_message_id="m-unk", status="unknown")
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "unk") == (0, None)


def test_emits_summary_json_with_per_id_lists(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    recent = (date.today() - timedelta(days=10)).isoformat()
    _insert(db_path, id="s1", email_message_id="m-s1", status="cancelled", order_date=recent)
    _insert(
        db_path,
        id="s2",
        email_message_id="m-s2",
        status="cancelled",
        order_date=(date.today() - timedelta(days=70)).isoformat(),
        flagged=1,
        flag_reason="Order cancelled",
    )
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["flagged"] == 1
    assert payload["unflagged"] == 1
    assert "s1" in payload["ids_flagged"]
    assert "s2" in payload["ids_unflagged"]
