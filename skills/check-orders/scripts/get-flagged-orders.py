#!/usr/bin/env python3
"""Fetch currently-flagged orders for the Step 11 alert message.

Step 11 of check-orders SKILL.md. Extracted from inline SKILL prose
per `coding-policy: script-delegation`.

Flagged rows sharing a `(source, order_number)` logical order are
collapsed to one — the confirmation and shipment emails of one order can
both be flagged, and the alert must show one line per order, not one per
email (`jbaruch/nanoclaw-orders#55`). The most-recent row (input is
ordered by `order_date` descending) is the representative; rows with a
NULL `order_number` cannot be paired and each stand alone.

Stdout on success: a single JSON array (possibly empty) of
`{description, flag_reason, source, order_date, merchant}` objects,
ordered by order_date descending. `merchant` (nullable) is surfaced so
the alert can identify a flagged item whose `source` is `other`.
`order_number` drives the collapse but is not part of the output shape.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


def main() -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT description, flag_reason, source, order_date, merchant,
                   order_number
              FROM orders
             WHERE flagged = 1
            ORDER BY order_date DESC
            """
        ).fetchall()
        seen_keys = set()
        result = []
        for row in rows:
            record = dict(row)
            order_number = record.pop("order_number")
            if isinstance(order_number, str) and order_number.strip():
                key = (record["source"], order_number.strip())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            result.append(record)
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"get-flagged-orders: SQLite error reading orders from "
            f"{DB_PATH}: {exc}. Verify the database file exists and "
            f"the orders table is present (created by the "
            f"orchestrator's state-001 migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
