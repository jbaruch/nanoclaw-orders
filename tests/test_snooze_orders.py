"""Tests for skills/check-orders/scripts/snooze-orders.py.

The script marks orders as owner-acknowledged-but-still-open by writing
`snooze_until`, leaving `status` untouched (`#63`, column from
`jbaruch/nanoclaw#917`). Step 8's detector then drops those rows from
`stuck_ids` until the window lapses.

`SNOOZE_UNTIL` is validated against the real clock (it must be in the
future), so tests pin `module.date` to a fixed-today subclass and use
literals relative to it — no dependence on the run date.
"""

import io
import json
import os
import pathlib
import sqlite3
import subprocess
from datetime import date

import pytest

FROZEN_TODAY = date(2026, 4, 30)

FUTURE = "2026-06-01"
PAST = "2026-04-01"


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
        "order_date": "2026-03-16",
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 1,
        "flag_reason": "Ordered, not yet shipped",
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


def _row(db_path, order_id) -> sqlite3.Row:
    """Fetch one orders row, failing the test if the id is absent.

    Every caller asserts on a row it just inserted, so a missing row is a
    broken test rather than a case to branch on — raising here keeps the
    call sites free of None-guards.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, flagged, flag_reason, snooze_until, last_updated "
            "FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"no orders row with id={order_id!r}")
        return row
    finally:
        conn.close()


def _run(module, monkeypatch, capsys, ids, until: str | None = FUTURE) -> tuple[int, dict]:
    """Run main() with a frozen clock, a set SNOOZE_UNTIL, and piped ids.

    Returns on the success path only: a rejected SNOOZE_UNTIL raises
    SystemExit out of `main()` before this ever returns, so the payload is
    never optional for callers that get one back.
    """
    monkeypatch.setattr(module, "date", _FrozenDate)
    if until is None:
        monkeypatch.delenv("SNOOZE_UNTIL", raising=False)
    else:
        monkeypatch.setenv("SNOOZE_UNTIL", until)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(ids)))
    code = module.main()
    out = capsys.readouterr()
    if not out.out.strip():
        raise AssertionError("snooze-orders produced no stdout on the success path")
    return code, json.loads(out.out)


def test_snoozes_an_ordered_row_without_touching_status(snooze_orders, monkeypatch, capsys):
    # The whole point: the row stays honestly `ordered` — `ack-orders.py`'s
    # `assumed_delivered` would record a delivery that never happened.
    module, db_path = snooze_orders
    _insert(db_path, id="ragnar", email_message_id="m-ragnar")

    code, payload = _run(module, monkeypatch, capsys, ["ragnar"])

    assert code == 0
    assert payload == {"snoozed": 1, "not_snoozed": 0, "snooze_until": FUTURE}
    row = _row(db_path, "ragnar")
    assert row["snooze_until"] == FUTURE
    assert row["status"] == "ordered"


def test_leaves_the_flag_alone(snooze_orders, monkeypatch, capsys):
    # A snooze says "stop asking", not "this is resolved" — clearing the
    # flag is `unflag-orders.py`'s job, and Step 9 recomputes it anyway.
    module, db_path = snooze_orders
    _insert(db_path, id="keep-flag", email_message_id="m-keep")

    _run(module, monkeypatch, capsys, ["keep-flag"])

    row = _row(db_path, "keep-flag")
    assert row["flagged"] == 1
    assert row["flag_reason"] == "Ordered, not yet shipped"


def test_stamps_last_updated(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="stamped", email_message_id="m-stamped")

    _run(module, monkeypatch, capsys, ["stamped"])

    assert _row(db_path, "stamped")["last_updated"] != "2026-04-01T00:00:00Z"


def test_snoozes_a_shipped_row(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="in-flight", email_message_id="m-flight", status="shipped")

    _code, payload = _run(module, monkeypatch, capsys, ["in-flight"])

    assert payload["snoozed"] == 1
    assert _row(db_path, "in-flight")["status"] == "shipped"


@pytest.mark.parametrize("terminal", ["cancelled", "refunded", "assumed_delivered"])
def test_terminal_rows_are_not_snoozed(snooze_orders, monkeypatch, capsys, terminal):
    # Snoozing is meaningful only for a live, in-flight order. A terminal
    # row is counted, never silently marked.
    module, db_path = snooze_orders
    _insert(db_path, id="done", email_message_id="m-done", status=terminal)

    _code, payload = _run(module, monkeypatch, capsys, ["done"])

    assert payload == {"snoozed": 0, "not_snoozed": 1, "snooze_until": FUTURE}
    assert _row(db_path, "done")["snooze_until"] is None


def test_unknown_id_counts_as_not_snoozed(snooze_orders, monkeypatch, capsys):
    module, _db_path = snooze_orders

    _code, payload = _run(module, monkeypatch, capsys, ["never-imported"])

    assert payload == {"snoozed": 0, "not_snoozed": 1, "snooze_until": FUTURE}


def test_mixed_batch_counts_both_sides(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="live", email_message_id="m-live")
    _insert(db_path, id="dead", email_message_id="m-dead", status="cancelled")

    _code, payload = _run(module, monkeypatch, capsys, ["live", "dead", "ghost"])

    assert payload == {"snoozed": 1, "not_snoozed": 2, "snooze_until": FUTURE}


def test_blank_lines_and_whitespace_are_tolerated(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="padded", email_message_id="m-padded")

    _code, payload = _run(module, monkeypatch, capsys, ["", "  padded  ", "", "   "])

    assert payload["snoozed"] == 1
    assert _row(db_path, "padded")["snooze_until"] == FUTURE


def test_empty_id_list_is_a_legal_no_op(snooze_orders, monkeypatch, capsys):
    module, _db_path = snooze_orders

    code, payload = _run(module, monkeypatch, capsys, [])

    assert code == 0
    assert payload == {"snoozed": 0, "not_snoozed": 0, "snooze_until": FUTURE}


def test_is_idempotent(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="twice", email_message_id="m-twice")

    _run(module, monkeypatch, capsys, ["twice"])
    _code, payload = _run(module, monkeypatch, capsys, ["twice"])

    assert payload["snoozed"] == 1
    assert _row(db_path, "twice")["snooze_until"] == FUTURE


def test_a_later_snooze_extends_the_window(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="extend", email_message_id="m-extend")

    _run(module, monkeypatch, capsys, ["extend"], until=FUTURE)
    _run(module, monkeypatch, capsys, ["extend"], until="2026-08-01")

    assert _row(db_path, "extend")["snooze_until"] == "2026-08-01"


def test_missing_snooze_until_exits_2(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="x", email_message_id="m-x")

    with pytest.raises(SystemExit) as excinfo:
        _run(module, monkeypatch, capsys, ["x"], until=None)

    assert excinfo.value.code == 2
    assert _row(db_path, "x")["snooze_until"] is None


def test_non_iso_snooze_until_exits_2(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="x", email_message_id="m-x")

    with pytest.raises(SystemExit) as excinfo:
        _run(module, monkeypatch, capsys, ["x"], until="next tuesday")

    assert excinfo.value.code == 2
    assert _row(db_path, "x")["snooze_until"] is None


@pytest.mark.parametrize("bad", [PAST, FROZEN_TODAY.isoformat()])
def test_non_future_snooze_until_exits_2(snooze_orders, monkeypatch, capsys, bad):
    # A past or same-day value would suppress nothing (the reader's test is
    # `today < snooze_until`), so writing it would leave the owner believing
    # an acknowledgement took effect when it did not.
    module, db_path = snooze_orders
    _insert(db_path, id="x", email_message_id="m-x")

    with pytest.raises(SystemExit) as excinfo:
        _run(module, monkeypatch, capsys, ["x"], until=bad)

    assert excinfo.value.code == 2
    assert _row(db_path, "x")["snooze_until"] is None


def test_rejects_before_reading_stdin(snooze_orders, monkeypatch, capsys):
    # Validation runs first, so a bad date never consumes the id list —
    # the operator can re-run the same pipe after fixing SNOOZE_UNTIL.
    module, _db_path = snooze_orders
    monkeypatch.setattr(module, "date", _FrozenDate)
    monkeypatch.setenv("SNOOZE_UNTIL", PAST)
    stdin = io.StringIO("some-id")
    monkeypatch.setattr("sys.stdin", stdin)

    with pytest.raises(SystemExit):
        module.main()

    assert stdin.tell() == 0


# --- The documented invocation actually works (#64 review) -------------

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "skills/check-orders/scripts/snooze-orders.py"
)


def _shell(command, db_path):
    """Run a real shell pipeline against the fixture DB, returning stderr."""
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, "ORDERS_DB_PATH": str(db_path)},
    )


def test_documented_pipeline_delivers_the_env_var(snooze_orders):
    # `VAR=x cmd1 | cmd2` assigns only to cmd1, so an invocation that put
    # SNOOZE_UNTIL before `printf` would leave the script seeing nothing
    # and exiting 2 — the shape the SKILL documented before #64's review.
    #
    # A deliberately PAST date keeps this deterministic: it never reaches
    # the database, and the two exit-2 messages are distinguishable, so
    # "the variable arrived" is provable without a future literal.
    _module, db_path = snooze_orders

    result = _shell(f"printf '%s\\n' some-id | SNOOZE_UNTIL={PAST} python3 {SCRIPT}", db_path)

    assert result.returncode == 2
    assert "is not in the future" in result.stderr
    assert "SNOOZE_UNTIL is required" not in result.stderr


def test_assignment_before_the_pipe_does_not_reach_the_script(snooze_orders):
    # Pins the failure mode itself, so the documented form cannot silently
    # regress back to it.
    _module, db_path = snooze_orders

    result = _shell(f"SNOOZE_UNTIL={PAST} printf '%s\\n' some-id | python3 {SCRIPT}", db_path)

    assert result.returncode == 2
    assert "SNOOZE_UNTIL is required" in result.stderr


@pytest.mark.parametrize(
    "noncanonical",
    ["20260601", "2026-W40-1", "2026-6-1", "2026/06/01"],
)
def test_noncanonical_iso_forms_exit_2(snooze_orders, monkeypatch, capsys, noncanonical):
    # `date.fromisoformat` would accept the basic and week forms and
    # normalize them into a stored snooze the stuck detector then ignores
    # (it honours the canonical form alone) — a silent no-op the owner
    # would believe had taken effect.
    module, db_path = snooze_orders
    _insert(db_path, id="x", email_message_id="m-x")

    with pytest.raises(SystemExit) as excinfo:
        _run(module, monkeypatch, capsys, ["x"], until=noncanonical)

    assert excinfo.value.code == 2
    assert _row(db_path, "x")["snooze_until"] is None


def test_impossible_calendar_date_exits_2(snooze_orders, monkeypatch, capsys):
    module, db_path = snooze_orders
    _insert(db_path, id="x", email_message_id="m-x")

    with pytest.raises(SystemExit) as excinfo:
        _run(module, monkeypatch, capsys, ["x"], until="2026-13-45")

    assert excinfo.value.code == 2
    assert _row(db_path, "x")["snooze_until"] is None
