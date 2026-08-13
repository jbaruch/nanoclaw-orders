#!/usr/bin/env python3
"""Acknowledge a set of orders as delivered, by id.

Owner-ack tool (`jbaruch/nanoclaw-orders#61`). When the owner says "these
arrived / I've got them" about flagged orders, the ack must be *persisted
where the stuck detector reads it* — otherwise the row stays `status =
'ordered'` and `compute-stuck-orders.py` (Step 8) re-flags it every run.
`unflag-orders.py` only clears `flagged`; the status stays `ordered`, so
a stuck-order alert the owner acked resurfaces the next night. This is the
"roach motel" half of #61.

This script transitions each acked row to the synthetic terminal
`assumed_delivered` (same status `promote-stale-shipped.py` uses), which
`compute-stuck-orders.py` treats as shipped and Step 9 never flags. The
row leaves the `ordered` pool for good, so the ack sticks.

Only `ordered`/`shipped` rows are eligible — acking is meaningful only for
a live, in-flight order. A `cancelled`/`refunded`/already-terminal row is
left untouched (counted as `not_acked`) rather than silently overwritten.

Reads ids from stdin (one per line, leading/trailing whitespace tolerated,
blank lines ignored). Each `UPDATE` is parameter-bound.

Stdout on success: `{"acked": <int>, "not_acked": <int>}`. `acked` counts
ids whose row was eligible and transitioned (`cursor.rowcount > 0`);
`not_acked` counts ids that matched no eligible row (stale id, never
imported, or already terminal — diagnostic, not an error).

This is an ad-hoc tool invoked outside the nightly flow, in response to an
owner acknowledgement — not a Step in the sequential check-orders run.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


def main() -> int:
    raw = sys.stdin.read()
    ids = [line.strip() for line in raw.splitlines() if line.strip()]
    if not ids:
        # No ids to ack — degenerate but legal.
        json.dump({"acked": 0, "not_acked": 0}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    acked = 0
    not_acked = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Single transaction so the ack set is all-or-nothing per
        # invocation — a transient failure mid-batch leaves the table
        # untouched and the agent can safely re-run with the same ids.
        with conn:
            for order_id in ids:
                cur = conn.execute(
                    """
                    UPDATE orders
                       SET status = 'assumed_delivered',
                           flagged = 0,
                           flag_reason = NULL,
                           last_updated = ?
                     WHERE id = ?
                       AND status IN ('ordered', 'shipped')
                    """,
                    (now_iso, order_id),
                )
                # rowcount is 1 when the id existed AND was in an ackable
                # status; 0 when the id is missing or already terminal.
                if cur.rowcount > 0:
                    acked += 1
                else:
                    not_acked += 1
        json.dump({"acked": acked, "not_acked": not_acked}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"ack-orders: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is writable, and the "
            f"orders table is present (created by the orchestrator's "
            f"state-001 migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
