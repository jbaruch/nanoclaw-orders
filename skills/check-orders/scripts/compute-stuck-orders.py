#!/usr/bin/env python3
"""Compute the ids of orders stuck in `ordered` with no shipment.

Step 8 of check-orders SKILL.md. Deterministic stuck-order detection
(`jbaruch/nanoclaw-orders#55`): an `ordered` row whose `order_date` falls
in the stuck window — at least STUCK_ORDER_MIN_DAYS and at most
STUCK_ORDER_MAX_DAYS before today, both bounds inclusive — is stuck unless
its logical order has a shipment row. Logical-order identity is the
persisted `(source, order_number)` key; a row whose `order_number` is NULL
cannot be paired to a shipment, so it is stuck on age alone.

Pairing is a deterministic join on the stored `order_number` column
(populated at ingestion, `#58`), so it lives in this script — the earlier
agent-pairing step is gone. The confirmation and shipment emails of one
order carry the same order number, so an order that shipped is not counted
as stuck.

Below STUCK_ORDER_MIN_DAYS a slow ship is normal, not yet a signal; above
STUCK_ORDER_MAX_DAYS the alert ages out (channel stays signal-only, same
philosophy as the flag-anomalies cutoffs).

Stdout on success: `{"stuck_ids": ["amazon-...", ...]}` (ascending id
order). The list feeds `flag-anomalies.py`'s STUCK_IDS at Step 9.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

STUCK_ORDER_MIN_DAYS = 7
STUCK_ORDER_MAX_DAYS = 90

_SHIPPED_STATUSES = ("shipped", "delivered", "assumed_delivered")


def _aged_candidate(order_date) -> bool:
    """True iff order_date parses as ISO date AND falls in the stuck window.

    Type-guards against SQLite's permissiveness (same rationale as
    flag-anomalies `_within_days`): a non-string or malformed value is
    ineligible rather than a crash.
    """
    if not isinstance(order_date, str) or not order_date.strip():
        return False
    try:
        parsed = date.fromisoformat(order_date.strip()[:10])
    except ValueError:
        return False
    delta = (date.today() - parsed).days
    return STUCK_ORDER_MIN_DAYS <= delta <= STUCK_ORDER_MAX_DAYS


def _order_key(source, order_number):
    """(source, order_number) logical-order key, or None when the row has
    no usable order number (NULL/blank) and so cannot be paired."""
    if isinstance(order_number, str) and order_number.strip():
        return (source, order_number.strip())
    return None


def main() -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, source, order_date, order_number FROM orders"
        ).fetchall()

        # Logical-order keys proven shipped. Built over all rows so an
        # excluded shipment still proves its `ordered` sibling shipped.
        shipped_keys = set()
        for row in rows:
            if row["status"] in _SHIPPED_STATUSES:
                key = _order_key(row["source"], row["order_number"])
                if key is not None:
                    shipped_keys.add(key)

        stuck_ids = []
        for row in rows:
            if row["status"] != "ordered" or not _aged_candidate(row["order_date"]):
                continue
            key = _order_key(row["source"], row["order_number"])
            if key is None or key not in shipped_keys:
                stuck_ids.append(row["id"])

        json.dump({"stuck_ids": sorted(stuck_ids)}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"compute-stuck-orders: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is readable, and the orders "
            f"table is present (created by the orchestrator's state-001 "
            f"migration, with order_number added by state-017).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
