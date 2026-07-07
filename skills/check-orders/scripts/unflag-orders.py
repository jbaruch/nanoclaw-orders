#!/usr/bin/env python3
"""Unflag a set of orders by id.

Step 6 of check-orders SKILL.md: orders matching a Step 6 exclusion
rule must be set to `flagged = 0`, `flag_reason = NULL` regardless of
their current state. Extracted from inline SKILL prose per
`coding-policy: script-delegation`.

Reads ids from stdin (one per line, leading/trailing whitespace
tolerated, blank lines ignored). Each `UPDATE` is parameter-bound so
ids are safe under any future change to the id format (today's
`{source}-{order_date}-{hash}` is hex-only, but the binding makes that
incidental).

Stdout on success: `{"unflagged_existing": <int>, "missing_ids": <int>}`.
`unflagged_existing` counts ids whose UPDATE matched a row (via
`cursor.rowcount > 0`); `missing_ids` counts ids that didn't match
any row (caller passed a stale or never-imported id — diagnostic, not
an error). Note an already-unflagged row still counts as "existing"
because the WHERE clause matched, even though no column changed.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


def main() -> int:
    raw = sys.stdin.read()
    ids = [line.strip() for line in raw.splitlines() if line.strip()]
    if not ids:
        # No exclusions to apply — degenerate but legal (the SKILL
        # may invoke this with an empty exclusion set when the rule
        # table doesn't match anything in this run).
        json.dump({"unflagged_existing": 0, "missing_ids": 0}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    unflagged = 0
    missing = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Single transaction so the unflag set is all-or-nothing per
        # invocation. The agent re-running with the same ids on a
        # transient failure won't double-count or partially apply.
        with conn:
            for order_id in ids:
                cur = conn.execute(
                    "UPDATE orders SET flagged = 0, flag_reason = NULL WHERE id = ?",
                    (order_id,),
                )
                # `cur.rowcount` returns the number of rows the WHERE
                # clause matched (1 when the id exists, 0 otherwise).
                # Note that `unflagged_existing` counts ids that
                # MATCHED a row — not ids whose flagged column changed
                # — so an already-unflagged row still counts here. The
                # docstring's earlier reference to `UPDATE ... RETURNING`
                # was a paste-over from a draft that never shipped.
                if cur.rowcount > 0:
                    unflagged += 1
                else:
                    missing += 1
        json.dump(
            {"unflagged_existing": unflagged, "missing_ids": missing},
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"unflag-orders: SQLite error against {DB_PATH}: {exc}. "
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
