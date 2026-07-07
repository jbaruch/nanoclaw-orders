"""Tests for skills/check-orders/scripts/promote-stale-shipped.py.

Locks down the script's contract:

  - eligibility: status IN ('shipped', 'ordered')
                 AND expected_delivery non-null AND >=10 days old (or malformed)
                 AND last_updated >=10 days ago
  - effect: UPDATE status='assumed_delivered', flagged=0, flag_reason=NULL,
            last_updated=now
  - stdout: `{"promoted": <int>, "ids": [...]}`
  - non-string last_updated must be defensively skipped (the original
    bug was AttributeError from .replace() before the ValueError handler)
  - exit codes: 0 success, 1 IO/schema error

Tests freeze `module.date` and `module.datetime` to fixed-now subclasses
(same pattern as test_within_days.py) so every fixture is a fixed
literal relative to FROZEN_TODAY / FROZEN_NOW and the 10-day boundary
tested never moves with the run date.
"""

import json
import sqlite3
from datetime import date, datetime, timezone

FROZEN_TODAY = date(2026, 4, 30)
FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

# Fixed literals relative to FROZEN_TODAY / FROZEN_NOW:
FIFTEEN_DAYS_AGO_DATE = "2026-04-15"
THREE_DAYS_AGO_DATE = "2026-04-27"
FIFTEEN_DAYS_AGO_DT = "2026-04-15T12:00:00+00:00"
TWO_DAYS_AGO_DT = "2026-04-28T12:00:00+00:00"


class _FrozenDate(date):
    """Subclass with a fixed `today()` for deterministic delta math."""

    @classmethod
    def today(cls):
        return FROZEN_TODAY


class _FrozenDatetime(datetime):
    """Subclass with a fixed `now()`; inherited `fromisoformat` still
    parses fixture strings, so `now() - parsed` stays deterministic."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


def _insert(db_path, **fields):
    defaults = {
        "id": "amazon-2026-01-01-aaaaaaaa",
        "source": "amazon",
        "status": "shipped",
        "amount": None,
        "currency": "USD",
        "description": "Thing",
        "order_date": "2026-01-01",
        "expected_delivery": None,
        "email_message_id": "msg-1",
        "to_address": None,
        "flagged": 0,
        "flag_reason": None,
        "last_updated": FROZEN_NOW.isoformat(),
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


def _status(db_path, order_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()[0]
    finally:
        conn.close()


def _run(module, monkeypatch, capsys):
    monkeypatch.setattr(module, "date", _FrozenDate)
    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    code = module.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def test_promotes_eligible_shipped_row(promote_stale_shipped, monkeypatch, capsys):
    module, db_path = promote_stale_shipped
    _insert(
        db_path,
        id="ord-1",
        email_message_id="msg-1",
        status="shipped",
        expected_delivery=FIFTEEN_DAYS_AGO_DATE,
        last_updated=FIFTEEN_DAYS_AGO_DT,
    )
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload == {"promoted": 1, "ids": ["ord-1"]}
    assert _status(db_path, "ord-1") == "assumed_delivered"


def test_skips_recently_updated_row(promote_stale_shipped, monkeypatch, capsys):
    module, db_path = promote_stale_shipped
    _insert(
        db_path,
        id="ord-2",
        email_message_id="msg-2",
        status="shipped",
        expected_delivery=FIFTEEN_DAYS_AGO_DATE,
        last_updated=TWO_DAYS_AGO_DT,
    )
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["promoted"] == 0
    assert _status(db_path, "ord-2") == "shipped"


def test_skips_recent_expected_delivery(promote_stale_shipped, monkeypatch, capsys):
    module, db_path = promote_stale_shipped
    _insert(
        db_path,
        id="ord-3",
        email_message_id="msg-3",
        status="shipped",
        expected_delivery=THREE_DAYS_AGO_DATE,
        last_updated=FIFTEEN_DAYS_AGO_DT,
    )
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["promoted"] == 0


def test_treats_malformed_expected_delivery_as_eligible(promote_stale_shipped, monkeypatch, capsys):
    """Per the original within-days.py contract, a malformed date string
    ('overnight', 'soon') counts as eligible — it's been long enough
    that no specific date is meaningful."""
    module, db_path = promote_stale_shipped
    _insert(
        db_path,
        id="ord-4",
        email_message_id="msg-4",
        status="shipped",
        expected_delivery="overnight",
        last_updated=FIFTEEN_DAYS_AGO_DT,
    )
    code, _out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert _status(db_path, "ord-4") == "assumed_delivered"


def test_skips_terminal_status(promote_stale_shipped, monkeypatch, capsys):
    module, db_path = promote_stale_shipped
    _insert(
        db_path,
        id="ord-5",
        email_message_id="msg-5",
        status="delivered",
        expected_delivery=FIFTEEN_DAYS_AGO_DATE,
        last_updated=FIFTEEN_DAYS_AGO_DT,
    )
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["promoted"] == 0


def test_handles_null_last_updated_without_attribute_error(
    promote_stale_shipped, monkeypatch, capsys
):
    """Regression for the gh-aw review catch on PR #109: a row where
    last_updated comes back as None (or any non-string) must skip with a
    diagnostic, not raise AttributeError from .replace().

    SQLite's NOT NULL constraint blocks a direct INSERT of NULL, so the
    test relaxes the schema for this row to construct the exact failure
    mode the production code must defend against — a column that
    returns Python None or a non-string at fetch time.
    """
    module, db_path = promote_stale_shipped
    conn = sqlite3.connect(str(db_path))
    try:
        # Recreate the orders table without the NOT NULL on
        # last_updated, so we can insert a row that reproduces the
        # `value` shape (None) that triggered AttributeError before
        # the fix.
        conn.executescript(
            """
            DROP TABLE orders;
            CREATE TABLE orders (
              id                TEXT PRIMARY KEY,
              source            TEXT NOT NULL,
              status            TEXT NOT NULL,
              amount            REAL,
              currency          TEXT,
              description       TEXT NOT NULL,
              order_date        TEXT NOT NULL,
              expected_delivery TEXT,
              email_message_id  TEXT NOT NULL UNIQUE,
              to_address        TEXT,
              flagged           INTEGER NOT NULL DEFAULT 0,
              flag_reason       TEXT,
              last_updated      TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO orders (id, source, status, description, order_date, "
            "email_message_id, expected_delivery, last_updated) "
            "VALUES ('ord-6', 'amazon', 'shipped', 'Thing', '2026-01-01', "
            "'msg-6', ?, NULL)",
            (FIFTEEN_DAYS_AGO_DATE,),
        )
        conn.commit()
    finally:
        conn.close()
    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["promoted"] == 0
    assert "non-string last_updated" in err


def test_no_op_when_table_is_empty(promote_stale_shipped, monkeypatch, capsys):
    module, _db_path = promote_stale_shipped
    code, out, _err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out) == {"promoted": 0, "ids": []}
