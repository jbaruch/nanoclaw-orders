"""Tests for skills/check-orders/scripts/flag-anomalies.py.

Covers each row of the Step 10 rule table:

  | status     | extra condition                       | flag_reason                | cutoff |
  |------------|---------------------------------------|----------------------------|--------|
  | cancelled  | -                                     | "Order cancelled"          | 14d    |
  | refunded   | -                                     | "Refund/return"            | 14d    |
  | shipped|ordered overdue expected_delivery          | "Overdue delivery"         | 30d    |
  | ordered    | id supplied in STUCK_IDS              | "Ordered, not yet shipped" | caller |

Stuck-order pairing moved to the agent (`#55`); this script trusts the
STUCK_IDS the agent supplies. The candidate window lives in
`list-stuck-candidates.py` (see test_list_stuck_candidates.py).

Plus the unflag-past-cutoff branch, the EXCLUDED_IDS env-var honour, and
the removal of the old "Large purchase" rule (`#55`).

Tests freeze `module.date` to a fixed-today subclass (same pattern as
test_within_days.py) so every fixture date is a fixed literal relative
to FROZEN_TODAY and the boundaries tested never move with the run date.
"""

import json
import sqlite3
from datetime import date

FROZEN_TODAY = date(2026, 4, 30)

# Fixed literals relative to FROZEN_TODAY (2026-04-30):
FIVE_DAYS_AGO = "2026-04-25"
TEN_DAYS_AGO = "2026-04-20"
TWENTY_DAYS_AGO = "2026-04-10"
FORTY_FIVE_DAYS_AGO = "2026-03-16"
SEVENTY_DAYS_AGO = "2026-02-19"


class _FrozenDate(date):
    """Subclass with a fixed `today()` for deterministic delta math."""

    @classmethod
    def today(cls):
        return FROZEN_TODAY


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "amazon",
        "status": "shipped",
        "amount": 0.0,
        "currency": "USD",
        "description": "Thing",
        "order_date": FROZEN_TODAY.isoformat(),
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


def _run(module, monkeypatch, capsys, excluded_ids="", stuck_ids=""):
    monkeypatch.setenv("EXCLUDED_IDS", excluded_ids)
    monkeypatch.setenv("STUCK_IDS", stuck_ids)
    monkeypatch.setattr(module, "date", _FrozenDate)
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_flags_cancelled_within_14_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(db_path, id="c1", email_message_id="m-c1", status="cancelled", order_date=TEN_DAYS_AGO)
    code, _out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    flagged, reason = _row(db_path, "c1")
    assert flagged == 1
    assert reason == "Order cancelled"


def test_unflags_cancelled_past_14_days(flag_anomalies, monkeypatch, capsys):
    # 20 days is the regression-relevant boundary: under the old 60d
    # cutoff this row would stay flagged; under 14d it must unflag.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="c2",
        email_message_id="m-c2",
        status="cancelled",
        order_date=TWENTY_DAYS_AGO,
        flagged=1,
        flag_reason="Order cancelled",
    )
    _run(module, monkeypatch, capsys)
    flagged, reason = _row(db_path, "c2")
    assert flagged == 0
    assert reason is None


def test_flags_refunded_within_14_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(db_path, id="r1", email_message_id="m-r1", status="refunded", order_date=TEN_DAYS_AGO)
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "r1") == (1, "Refund/return")


def test_unflags_refunded_past_14_days(flag_anomalies, monkeypatch, capsys):
    # Mirror of the cancelled past-cutoff case: 20 days would stay
    # flagged under the old 60d window and must unflag under 14d.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="r2",
        email_message_id="m-r2",
        status="refunded",
        order_date=TWENTY_DAYS_AGO,
        flagged=1,
        flag_reason="Refund/return",
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "r2") == (0, None)


def test_large_purchase_rule_removed_does_not_flag(flag_anomalies, monkeypatch, capsys):
    # `#55`: a large self-made purchase is no longer an anomaly. A shipped
    # row over the old $200 threshold, with no overdue date, must not flag.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="lp1",
        email_message_id="m-lp1",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        amount=1517.33,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "lp1") == (0, None)


def test_existing_large_purchase_flag_is_cleared(flag_anomalies, monkeypatch, capsys):
    # `#55`: rows carrying a legacy "Large purchase" reason unflag on the
    # next pass now that the rule is gone.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="lp2",
        email_message_id="m-lp2",
        status="delivered",
        order_date=FIVE_DAYS_AGO,
        amount=300.0,
        flagged=1,
        flag_reason="Large purchase: $300.00",
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "lp2") == (0, None)


def test_flags_overdue_shipped_within_30_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="od1",
        email_message_id="m-od1",
        status="shipped",
        expected_delivery=TEN_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "od1") == (1, "Overdue delivery")


def test_does_not_flag_overdue_past_30_days(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="od2",
        email_message_id="m-od2",
        status="shipped",
        expected_delivery=FORTY_FIVE_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "od2") == (0, None)


def test_excluded_ids_env_var_skips_flagging(flag_anomalies, monkeypatch, capsys):
    """Step 6 exclusions: ids passed via EXCLUDED_IDS must not be re-flagged."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="ex1",
        email_message_id="m-ex1",
        status="cancelled",
        order_date=TEN_DAYS_AGO,
    )
    # Without exclusion: would flag.
    _run(module, monkeypatch, capsys, excluded_ids="ex1")
    assert _row(db_path, "ex1") == (0, None)


def test_flags_ordered_supplied_in_stuck_ids(flag_anomalies, monkeypatch, capsys):
    # `#55` primary signal: the agent (Step 9) paired this aged `ordered`
    # row and found no shipment, so it arrives here in STUCK_IDS.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="stuck1",
        email_message_id="m-stuck1",
        status="ordered",
        description="Stoiq Carry-On 17L",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys, stuck_ids="stuck1")
    assert _row(db_path, "stuck1") == (1, "Ordered, not yet shipped")


def test_ordered_not_in_stuck_ids_is_not_flagged(flag_anomalies, monkeypatch, capsys):
    # An `ordered` row the agent did not mark stuck (paired to a shipment,
    # or too fresh/old for the candidate window) must not flag.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="ord1",
        email_message_id="m-ord1",
        status="ordered",
        description="your order W1584689498",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys, stuck_ids="")
    assert _row(db_path, "ord1") == (0, None)


def test_unflags_previously_stuck_not_in_stuck_ids(flag_anomalies, monkeypatch, capsys):
    # A row flagged stuck last pass that the agent no longer lists (it
    # shipped, or aged out of the candidate window) unflags.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="stuck2",
        email_message_id="m-stuck2",
        status="ordered",
        description="Old thing",
        order_date=FORTY_FIVE_DAYS_AGO,
        flagged=1,
        flag_reason="Ordered, not yet shipped",
    )
    _run(module, monkeypatch, capsys, stuck_ids="")
    assert _row(db_path, "stuck2") == (0, None)


def test_overdue_delivery_outranks_supplied_stuck(flag_anomalies, monkeypatch, capsys):
    # An `ordered` row with a concrete overdue expected_delivery keeps the
    # more specific "Overdue delivery" reason even when supplied as stuck.
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="both",
        email_message_id="m-both",
        status="ordered",
        description="Ragnar Armoury #140898",
        order_date=FORTY_FIVE_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys, stuck_ids="both")
    assert _row(db_path, "both") == (1, "Overdue delivery")


def test_status_unknown_does_not_flag(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(db_path, id="unk", email_message_id="m-unk", status="unknown")
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "unk") == (0, None)


def test_emits_summary_json_with_per_id_lists(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(db_path, id="s1", email_message_id="m-s1", status="cancelled", order_date=TEN_DAYS_AGO)
    _insert(
        db_path,
        id="s2",
        email_message_id="m-s2",
        status="cancelled",
        order_date=SEVENTY_DAYS_AGO,
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
