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


def _insert(db_path, *, snooze_column=True, **fields):
    """Insert one orders row, defaulting every column the tests don't set.

    Columns are derived from the dict rather than a hand-maintained literal
    list, so adding a default needs no parallel edit to the SQL.
    `snooze_column=False` targets the pre-state-018 table the
    `compute_stuck_orders_legacy` fixture builds, which has no
    `snooze_until` column to insert into.
    """
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
        "merchant": None,
        "order_number": None,
    }
    if snooze_column:
        defaults["snooze_until"] = None
    defaults.update(fields)
    columns = ", ".join(defaults)
    placeholders = ", ".join("?" * len(defaults))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            f"INSERT INTO orders ({columns}) VALUES ({placeholders})",
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


# --- Never-ship merchants (#63) ---------------------------------------


def test_never_ship_merchant_is_not_stuck(compute_stuck_orders, monkeypatch, capsys):
    # A Kickstarter pledge never emits a shipment email, so "ordered with
    # no shipment" is its steady state — not an anomaly worth an alert.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="ks",
        email_message_id="m-ks",
        merchant="Kickstarter",
        description="Pledge: mechanical keyboard",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": []}


def test_never_ship_match_is_case_insensitive(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="pat",
        email_message_id="m-pat",
        merchant="PATREON MEMBERSHIP",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


def test_shipping_merchant_stays_stuck(compute_stuck_orders, monkeypatch, capsys):
    # Guards the suppression against over-reach: a normal merchant is
    # unaffected by the never-ship set.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="pacagen",
        email_message_id="m-pac",
        merchant="Pacagen",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["pacagen"]}


def test_populated_merchant_beats_description_mention(compute_stuck_orders, monkeypatch, capsys):
    # Precedence mirrors apply-exclusions.py: a populated merchant is
    # authoritative, so a description that merely name-drops a never-ship
    # merchant cannot suppress a real Amazon order.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="amz",
        email_message_id="m-amz",
        merchant="Amazon",
        description="Keyboard bought with my Kickstarter refund",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["amz"]}


def test_null_merchant_falls_back_to_description(compute_stuck_orders, monkeypatch, capsys):
    # Legacy rows predating state-017 have no merchant; on an unclassified
    # source the description is the only signal available for them.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="legacy-ks",
        email_message_id="m-legacy-ks",
        source="other",
        merchant=None,
        description="Indiegogo campaign — solar lamp",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


def test_known_shipping_source_ignores_the_description_fallback(
    compute_stuck_orders, monkeypatch, capsys
):
    # An amazon row can legitimately carry merchant=NULL. Without gating
    # the fallback to the unclassified source, a description merely
    # mentioning a pledge would drop a real stuck order out of the pool.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="amz-null-merchant",
        email_message_id="m-amz-null",
        source="amazon",
        merchant=None,
        description="Keyboard bought with my Kickstarter refund",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["amz-null-merchant"]}


def test_populated_never_ship_merchant_wins_on_any_source(
    compute_stuck_orders, monkeypatch, capsys
):
    # The source gate covers only the description fallback. A row whose
    # merchant column actually says Kickstarter is suppressed wherever it
    # classified.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="shopify-ks",
        email_message_id="m-shopify-ks",
        source="shopify",
        merchant="Kickstarter",
        description="Pledge fulfilment",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": []}


# --- Snooze window (#63 / jbaruch/nanoclaw#917) ------------------------


def test_open_snooze_window_suppresses(compute_stuck_orders, monkeypatch, capsys):
    # The Ragnar case: genuinely not shipped, owner-acknowledged. The row
    # keeps status='ordered' and simply stops being reported.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="ragnar",
        email_message_id="m-ragnar",
        description="Ragnar Armoury — truly not shipped",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until="2026-06-01",  # after FROZEN_TODAY (2026-04-30)
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": []}


def test_lapsed_snooze_window_reflags(compute_stuck_orders, monkeypatch, capsys):
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="lapsed",
        email_message_id="m-lapsed",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until="2026-04-01",  # before FROZEN_TODAY
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["lapsed"]}


def test_snooze_boundary_day_reflags(compute_stuck_orders, monkeypatch, capsys):
    # Suppression is `today < snooze_until`, so a snooze "until 2026-04-30"
    # is over ON 2026-04-30 rather than lasting one day longer.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="boundary",
        email_message_id="m-boundary",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until=FROZEN_TODAY.isoformat(),
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["boundary"]}


def test_malformed_snooze_does_not_suppress(compute_stuck_orders, monkeypatch, capsys):
    # A bad marker must never silently swallow a real alert — it degrades
    # to "not snoozed" rather than crashing or suppressing.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="garbage",
        email_message_id="m-garbage",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until="soon-ish",
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": ["garbage"]}


def test_runs_against_pre_state_018_schema(compute_stuck_orders_legacy, monkeypatch, capsys):
    # Cross-pipeline reader discipline: the tile keeps working against a
    # database the orchestrator has not migrated yet. An absent column
    # means "nothing is snoozed", never an error.
    module, db_path = compute_stuck_orders_legacy
    _insert(
        db_path,
        snooze_column=False,
        id="unmigrated",
        email_message_id="m-unmigrated",
        order_date=FORTY_FIVE_DAYS_AGO,
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": ["unmigrated"]}


def test_iso_prefix_with_trailing_garbage_does_not_suppress(
    compute_stuck_orders, monkeypatch, capsys
):
    # A leading-10-character slice would parse this as 2026-06-01 and
    # suppress the order, contradicting the contract that a malformed
    # marker cannot hide a real alert. `snooze_until` is only ever written
    # as a bare YYYY-MM-DD, so the whole value must parse.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="prefix-garbage",
        email_message_id="m-prefix-garbage",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until="2026-06-01garbage",
    )
    code, payload = _run(module, monkeypatch, capsys)
    assert code == 0
    assert payload == {"stuck_ids": ["prefix-garbage"]}


def test_timestamp_shaped_snooze_does_not_suppress(compute_stuck_orders, monkeypatch, capsys):
    # Same rule from the other side: a full ISO timestamp is not the
    # documented shape for this column, so it does not suppress either.
    module, db_path = compute_stuck_orders
    _insert(
        db_path,
        id="timestamped",
        email_message_id="m-timestamped",
        order_date=FORTY_FIVE_DAYS_AGO,
        snooze_until="2026-06-01T00:00:00Z",
    )
    _code, payload = _run(module, monkeypatch, capsys)
    assert payload == {"stuck_ids": ["timestamped"]}
