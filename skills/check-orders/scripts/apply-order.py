#!/usr/bin/env python3
"""Upsert one order record into the `orders` table.

Stdin contract: a single JSON object with the parsed-email fields produced
by Step 4 of `check-orders/SKILL.md`, plus the pre-computed `id` (output
of `compute-order-id.py`). Required keys:

    id, source, status, description, order_date, email_message_id

Optional keys (default `None`/`null` when omitted):

    amount, currency, expected_delivery, to_address

Stdout on success: a single JSON object describing what happened —
`{"action": "inserted" | "status_updated" | "noop", "id": "..."}`. The
`status_updated` case fires when an existing row's status changed; `noop`
fires when the row already existed at the same status (idempotent re-run).

The UPSERT itself is parameter-bound via `sqlite3` so descriptions
containing apostrophes can't escape the string literal — the whole reason
this script exists rather than letting SKILL.md interpolate values into a
heredoc'd SQL statement (the JSON era used `merge-orders-db.py` for the
same reason).

Exit codes: 0 success, 1 schema/IO failure (diagnostic on stderr), 2
usage error (no JSON on stdin or missing required field).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

REQUIRED_FIELDS = (
    "id",
    "source",
    "status",
    "description",
    "order_date",
    "email_message_id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("apply-order.py: no JSON on stdin\n")
        return 2
    try:
        order = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"apply-order.py: invalid JSON on stdin: {exc}\n")
        return 2
    if not isinstance(order, dict):
        sys.stderr.write("apply-order.py: stdin payload must be a JSON object\n")
        return 2
    missing = [k for k in REQUIRED_FIELDS if not order.get(k)]
    if missing:
        sys.stderr.write(f"apply-order.py: missing required field(s): {', '.join(missing)}\n")
        return 2

    last_updated = _now_iso()
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Resolve the PRE-existing status (if any) BEFORE the upsert, so the
        # action label we emit can distinguish "this row's status changed"
        # from "we already had this row at the same status". Doing this
        # post-upsert would mean re-reading the just-written row, which loses
        # the differentiation entirely.
        cur = conn.execute(
            "SELECT status FROM orders WHERE email_message_id = ?",
            (order["email_message_id"],),
        )
        prior = cur.fetchone()
        prior_status = prior[0] if prior else None

        conn.execute(
            """
            INSERT INTO orders (
              id, source, status, amount, currency, description, order_date,
              expected_delivery, email_message_id, to_address, flagged,
              flag_reason, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
            ON CONFLICT(email_message_id) DO UPDATE SET
              status       = excluded.status,
              last_updated = excluded.last_updated
            WHERE orders.status != excluded.status
            """,
            (
                order["id"],
                order["source"],
                order["status"],
                order.get("amount"),
                order.get("currency"),
                order["description"],
                order["order_date"],
                order.get("expected_delivery"),
                order["email_message_id"],
                order.get("to_address"),
                last_updated,
            ),
        )
        conn.commit()

        if prior is None:
            action = "inserted"
        elif prior_status != order["status"]:
            action = "status_updated"
        else:
            action = "noop"

        json.dump({"action": action, "id": order["id"]}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"apply-order.py: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is writable, and the "
            f"orders table is present (created by the orchestrator's "
            f"state-001 migration). If a row with the same `id` "
            f"({order['id']!r}) but a different email_message_id "
            f"already exists, this INSERT raises a PK collision; "
            f"that's an extremely-rare same-product-same-day case and "
            f"the operator should investigate the duplicate manually.\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
