"""Tests for skills/check-orders/scripts/flag-anomalies.py.

Covers each row of the Step 10 rule table:

  | status     | extra condition                       | flag_reason                | cutoff |
  |------------|---------------------------------------|----------------------------|--------|
  | cancelled  | -                                     | "Order cancelled"          | 14d    |
  | refunded   | -                                     | "Refund/return"            | 14d    |
  | shipped|ordered overdue expected_delivery,         | "Overdue delivery"         | 30d    |
  |            logical order not superseded (`#68`)   |                            |        |
  | ordered    | id supplied in STUCK_IDS              | "Ordered, not yet shipped" | caller |

Stuck-order detection lives in `compute-stuck-orders.py` (`#55`); this
script trusts the STUCK_IDS that script supplies (see
test_compute_stuck_orders.py).

Supersession (`#68`) is derived here, not supplied: one logical order can
split across two rows when the shipment email's description differs from the
confirmation's, and the stale `ordered` row goes on flagging "Overdue
delivery" after the order shipped. The suppression tests below pin both
halves — the split pair goes quiet, and the cases that must keep alerting
(no `order_number`, an earlier shipment, another source, a same-status
duplicate) still do.

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


# `#68`'s real Amazon order number: the confirmation and shipment emails
# carry it identically, which is what makes the two rows pairable at all.
ORDER_NUMBER = "111-7318829-3305816"


def test_overdue_suppressed_by_shipment_row_of_same_order(flag_anomalies, monkeypatch, capsys):
    """`#68` verbatim: one Amazon order, two rows, because the shipment
    email's description ("1 Essentials item") differs from the
    confirmation's ("Essentials item") and the id derives from it. The
    stale `ordered` row's expected_delivery is meaningless once the
    shipment row exists — it must go quiet."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="split-ordered",
        email_message_id="m-split-1",
        status="ordered",
        description="Essentials item",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="split-shipped",
        email_message_id="m-split-2",
        status="shipped",
        description="1 Essentials item",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "split-ordered") == (0, None)


def test_unflags_previously_flagged_superseded_row(flag_anomalies, monkeypatch, capsys):
    """The row `#68` reports is already flagged from an earlier pass; the
    first pass after the fix has to clear it, not just stop re-flagging."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="split-stale",
        email_message_id="m-stale-1",
        status="ordered",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
        flagged=1,
        flag_reason="Overdue delivery",
    )
    _insert(
        db_path,
        id="split-fresh",
        email_message_id="m-stale-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _code, out, _err = _run(module, monkeypatch, capsys)
    assert _row(db_path, "split-stale") == (0, None)
    assert "split-stale" in json.loads(out)["ids_unflagged"]


def test_supersession_holds_on_a_same_day_shipment(flag_anomalies, monkeypatch, capsys):
    """Same-day confirmation and shipment still pair: the date test is
    "no earlier", not "strictly later"."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="sameday-ordered",
        email_message_id="m-sameday-1",
        status="ordered",
        order_date=TEN_DAYS_AGO,
        expected_delivery=FIVE_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="sameday-shipped",
        email_message_id="m-sameday-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "sameday-ordered") == (0, None)


def test_excluded_shipment_row_still_supersedes(flag_anomalies, monkeypatch, capsys):
    """A Step 6 exclusion suppresses the shipment row's own flagging, not
    its evidence that the order shipped."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="exsup-ordered",
        email_message_id="m-exsup-1",
        status="ordered",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="exsup-shipped",
        email_message_id="m-exsup-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys, excluded_ids="exsup-shipped")
    assert _row(db_path, "exsup-ordered") == (0, None)


def test_delivered_row_supersedes_overdue_shipped_row(flag_anomalies, monkeypatch, capsys):
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="dlv-shipped",
        email_message_id="m-dlv-1",
        status="shipped",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="dlv-delivered",
        email_message_id="m-dlv-2",
        status="delivered",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "dlv-shipped") == (0, None)


def test_assumed_delivered_row_supersedes_overdue_ordered_row(flag_anomalies, monkeypatch, capsys):
    """`ack-orders.py`'s terminal status counts as arrival evidence too."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="ack-ordered",
        email_message_id="m-ack-1",
        status="ordered",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="ack-assumed",
        email_message_id="m-ack-2",
        status="assumed_delivered",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "ack-ordered") == (0, None)


def test_overdue_still_flags_without_an_order_number(flag_anomalies, monkeypatch, capsys):
    """A NULL `order_number` cannot be paired, so the pre-`#68` behaviour
    stands — the alternative would be silence on unpairable rows."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="nonum-ordered",
        email_message_id="m-nonum-1",
        status="ordered",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
    )
    _insert(
        db_path,
        id="nonum-shipped",
        email_message_id="m-nonum-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "nonum-ordered") == (1, "Overdue delivery")


def test_overdue_flags_when_the_shipment_predates_the_order(flag_anomalies, monkeypatch, capsys):
    """An older shipment cannot vouch for a later order that reuses the
    merchant's order number."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="older-ordered",
        email_message_id="m-older-1",
        status="ordered",
        order_date=TEN_DAYS_AGO,
        expected_delivery=FIVE_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="older-shipped",
        email_message_id="m-older-2",
        status="shipped",
        order_date=TWENTY_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "older-ordered") == (1, "Overdue delivery")


def test_overdue_flags_when_the_shipment_is_another_source(flag_anomalies, monkeypatch, capsys):
    """Logical-order identity is `(source, order_number)` — a Shopify order
    numbered the same as an Amazon one is a different order."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="xsrc-ordered",
        email_message_id="m-xsrc-1",
        status="ordered",
        source="amazon",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="xsrc-shipped",
        email_message_id="m-xsrc-2",
        status="shipped",
        source="shopify",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "xsrc-ordered") == (1, "Overdue delivery")


def test_duplicate_shipped_rows_do_not_silence_each_other(flag_anomalies, monkeypatch, capsys):
    """Supersession needs a strictly further-along status. Two `shipped`
    rows of one order are the same split-row accident, and treating either
    as evidence for the other would take a genuinely overdue delivery
    silent — the failure mode this rule must never have."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="dup-shipped-1",
        email_message_id="m-dup-1",
        status="shipped",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="dup-shipped-2",
        email_message_id="m-dup-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "dup-shipped-1") == (1, "Overdue delivery")


def test_unreadable_order_date_does_not_supersede(flag_anomalies, monkeypatch, capsys):
    """A date that cannot be compared must not be the reason an order goes
    quiet — same degrade-to-noisy stance the other type guards take."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="baddate-ordered",
        email_message_id="m-baddate-1",
        status="ordered",
        order_date=TWENTY_DAYS_AGO,
        expected_delivery=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="baddate-shipped",
        email_message_id="m-baddate-2",
        status="shipped",
        order_date="delivery pending",
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys)
    assert _row(db_path, "baddate-ordered") == (1, "Overdue delivery")


def test_supersession_does_not_gate_the_supplied_stuck_signal(flag_anomalies, monkeypatch, capsys):
    """Only the Overdue rule consults supersession. Step 8 owns the stuck
    pairing, and this script flags the ids it is handed verbatim."""
    module, db_path = flag_anomalies
    _insert(
        db_path,
        id="stuck-superseded",
        email_message_id="m-stucksup-1",
        status="ordered",
        order_date=FORTY_FIVE_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _insert(
        db_path,
        id="stuck-shipment",
        email_message_id="m-stucksup-2",
        status="shipped",
        order_date=TEN_DAYS_AGO,
        order_number=ORDER_NUMBER,
    )
    _run(module, monkeypatch, capsys, stuck_ids="stuck-superseded")
    assert _row(db_path, "stuck-superseded") == (1, "Ordered, not yet shipped")


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
