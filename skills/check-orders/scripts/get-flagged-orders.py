#!/usr/bin/env python3
"""Fetch currently-flagged orders for the Step 12 alert message.

Step 12 of check-orders SKILL.md. Extracted from inline SKILL prose
per `coding-policy: script-delegation`. Returns the same row shape
as `tessl__morning-brief/scripts/fetch-flagged-orders.py` because
both rendering paths consume the same fields (description,
flag_reason, source, order_date) — the difference is just which
caller chooses to send the resulting message.

Stdout on success: a single JSON array (possibly empty) of
`{description, flag_reason, source, order_date}` objects, ordered by
order_date descending.

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
            SELECT description, flag_reason, source, order_date
              FROM orders
             WHERE flagged = 1
            ORDER BY order_date DESC
            """
        ).fetchall()
        json.dump([dict(r) for r in rows], sys.stdout)
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
