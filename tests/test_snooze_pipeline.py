"""End-to-end tests for the snooze path across the three scripts.

The snooze contract spans `snooze-orders.py` (writes the marker),
`compute-stuck-orders.py` (Step 8, drops the row from `stuck_ids`), and
`flag-anomalies.py` (Step 9, suppresses every rule for it). Asserting only
that the writer stored a value proves nothing about the outcome the owner
cares about — that the order stops alerting — so these run the real
sequence against one shared database (`#63` review).

Two rows in particular could report `snoozed: 1` and still surface at
Step 11 if suppression lived in Step 8 alone:

  - an `ordered` row with an overdue `expected_delivery`, which the
    higher-priority "Overdue delivery" rule flags before the stuck rule
    is ever consulted
  - a `shipped` row, which `snooze-orders.py` accepts but Step 8 never
    examines (it considers `ordered` only)

Clocks are frozen on every module so nothing depends on the run date.
"""

import io
import json
import sqlite3
from datetime import date, timedelta

import pytest

FROZEN_TODAY = date(2026, 4, 30)

# Derived from the frozen reference per testing-standards Determinism.
SNOOZE_OPEN = (FROZEN_TODAY + timedelta(days=32)).isoformat()
SNOOZE_LAPSED = (FROZEN_TODAY - timedelta(days=29)).isoformat()
FORTY_FIVE_DAYS_AGO = (FROZEN_TODAY - timedelta(days=45)).isoformat()
OVERDUE_DELIVERY = (FROZEN_TODAY - timedelta(days=10)).isoformat()


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return FROZEN_TODAY


def _insert(db_path, **fields):
    defaults = {
        "id": "ord-1",
        "source": "other",
        "status": "ordered",
        "amount": 0.0,
        "currency": "USD",
        "description": "Ragnar Armoury helmet",
        "order_date": FORTY_FIVE_DAYS_AGO,
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 0,
        "flag_reason": None,
        "last_updated": "2026-04-01T00:00:00Z",
        "merchant": None,
        "snooze_until": None,
        "order_number": None,
    }
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


def _flag_state(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT flagged, flag_reason, status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"no orders row with id={order_id!r}")
        return row
    finally:
        conn.close()


def _run_nightly(stuck, flag, monkeypatch, capsys):
    """Run Step 8 then Step 9 exactly as the SKILL sequences them."""
    monkeypatch.setattr(stuck, "date", _FrozenDate)
    monkeypatch.setattr(flag, "date", _FrozenDate)

    assert stuck.main() == 0
    stuck_ids = json.loads(capsys.readouterr().out)["stuck_ids"]

    monkeypatch.setenv("STUCK_IDS", ",".join(stuck_ids))
    monkeypatch.setenv("EXCLUDED_IDS", "")
    assert flag.main() == 0
    return stuck_ids, json.loads(capsys.readouterr().out)


def _snooze(snooze, monkeypatch, capsys, ids, until=SNOOZE_OPEN):
    monkeypatch.setattr(snooze, "date", _FrozenDate)
    monkeypatch.setenv("SNOOZE_UNTIL", until)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(ids)))
    assert snooze.main() == 0
    return json.loads(capsys.readouterr().out)


def test_snoozed_stuck_order_stops_alerting(snooze_pipeline, monkeypatch, capsys):
    snooze, stuck, flag, db_path = snooze_pipeline
    _insert(db_path, id="ragnar", email_message_id="m-ragnar")

    # Baseline: without a snooze the nightly run flags it.
    stuck_ids, _ = _run_nightly(stuck, flag, monkeypatch, capsys)
    assert stuck_ids == ["ragnar"]
    assert _flag_state(db_path, "ragnar")["flagged"] == 1

    payload = _snooze(snooze, monkeypatch, capsys, ["ragnar"])
    assert payload["snoozed"] == 1

    stuck_ids, _ = _run_nightly(stuck, flag, monkeypatch, capsys)
    assert stuck_ids == []
    row = _flag_state(db_path, "ragnar")
    assert row["flagged"] == 0
    assert row["flag_reason"] is None
    # ...and the status stayed honest throughout.
    assert row["status"] == "ordered"


def test_snooze_suppresses_the_overdue_delivery_rule(snooze_pipeline, monkeypatch, capsys):
    # "Overdue delivery" outranks the stuck rule, so a snooze honoured
    # only in Step 8 would leave this row alerting.
    snooze, stuck, flag, db_path = snooze_pipeline
    _insert(
        db_path,
        id="overdue",
        email_message_id="m-overdue",
        expected_delivery=OVERDUE_DELIVERY,
    )

    _run_nightly(stuck, flag, monkeypatch, capsys)
    assert _flag_state(db_path, "overdue")["flag_reason"] == "Overdue delivery"

    _snooze(snooze, monkeypatch, capsys, ["overdue"])
    _run_nightly(stuck, flag, monkeypatch, capsys)

    assert _flag_state(db_path, "overdue")["flagged"] == 0


def test_snooze_suppresses_a_shipped_row(snooze_pipeline, monkeypatch, capsys):
    # `snooze-orders.py` accepts `shipped`, but Step 8 only examines
    # `ordered` — so this row's snooze is meaningful only because Step 9
    # honours it too.
    snooze, stuck, flag, db_path = snooze_pipeline
    _insert(
        db_path,
        id="in-flight",
        email_message_id="m-flight",
        status="shipped",
        expected_delivery=OVERDUE_DELIVERY,
    )

    _run_nightly(stuck, flag, monkeypatch, capsys)
    assert _flag_state(db_path, "in-flight")["flagged"] == 1

    payload = _snooze(snooze, monkeypatch, capsys, ["in-flight"])
    assert payload["snoozed"] == 1

    _run_nightly(stuck, flag, monkeypatch, capsys)
    assert _flag_state(db_path, "in-flight")["flagged"] == 0


def test_lapsed_snooze_alerts_again(snooze_pipeline, monkeypatch, capsys):
    # Suppression is a window, not a tombstone.
    _snooze_unused, stuck, flag, db_path = snooze_pipeline
    _insert(
        db_path,
        id="lapsed",
        email_message_id="m-lapsed",
        snooze_until=SNOOZE_LAPSED,
    )

    stuck_ids, _ = _run_nightly(stuck, flag, monkeypatch, capsys)

    assert stuck_ids == ["lapsed"]
    assert _flag_state(db_path, "lapsed")["flag_reason"] == "Ordered, not yet shipped"


@pytest.mark.parametrize("terminal_reason", ["cancelled", "refunded"])
def test_snooze_suppresses_terminal_status_rules(
    snooze_pipeline, monkeypatch, capsys, terminal_reason
):
    # A snoozed row is silent whichever rule would have fired. These
    # statuses are not snoozable through the writer (it accepts
    # ordered/shipped only), but a row can reach them after being
    # snoozed, and the owner's "stop asking" still holds.
    _snooze_unused, stuck, flag, db_path = snooze_pipeline
    _insert(
        db_path,
        id="terminal",
        email_message_id="m-terminal",
        status=terminal_reason,
        order_date=(FROZEN_TODAY - timedelta(days=3)).isoformat(),
        snooze_until=SNOOZE_OPEN,
    )

    _run_nightly(stuck, flag, monkeypatch, capsys)

    assert _flag_state(db_path, "terminal")["flagged"] == 0


def test_unsnoozed_neighbour_still_alerts(snooze_pipeline, monkeypatch, capsys):
    # Guards the suppression against over-reach across rows.
    snooze, stuck, flag, db_path = snooze_pipeline
    _insert(db_path, id="snoozed", email_message_id="m-snoozed")
    _insert(db_path, id="loud", email_message_id="m-loud")

    _snooze(snooze, monkeypatch, capsys, ["snoozed"])
    stuck_ids, _ = _run_nightly(stuck, flag, monkeypatch, capsys)

    assert stuck_ids == ["loud"]
    assert _flag_state(db_path, "snoozed")["flagged"] == 0
    assert _flag_state(db_path, "loud")["flagged"] == 1
