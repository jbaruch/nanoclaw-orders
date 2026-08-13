#!/usr/bin/env python3
"""Snooze a set of orders past a date, by id, without touching status.

Owner-ack tool for the *genuinely stuck* case (`jbaruch/nanoclaw-orders#63`,
column from `jbaruch/nanoclaw#917`). `ack-orders.py` answers "these
arrived" by transitioning rows to `assumed_delivered`. It cannot answer the
other half of the same owner reply — "this one truly not shipped, all the
rest shipped and delivered" (2026-08-10, the Ragnar Armoury case). Marking
that row `assumed_delivered` records a delivery that never happened;
leaving it `ordered` re-flags it every night.

This script writes `snooze_until` instead, so the row keeps its honest
`status = 'ordered'` while `compute-stuck-orders.py` (Step 8) drops it from
`stuck_ids` until the window lapses. Status, `flagged`, and `flag_reason`
are deliberately left alone: a snooze says "stop asking", not "this is
resolved".

Only `ordered`/`shipped` rows are eligible — snoozing is meaningful only
for a live, in-flight order. A `cancelled`/`refunded`/already-terminal row
is left untouched (counted as `not_snoozed`) rather than silently marked.

Inputs:
  - ids on stdin, one per line (leading/trailing whitespace tolerated,
    blank lines ignored)
  - `SNOOZE_UNTIL` env var: the ISO date (`YYYY-MM-DD`) the suppression
    runs until, exclusive — `compute-stuck-orders.py` re-flags ON that
    date. Must parse as an ISO date and be strictly in the future; a past
    or same-day value is rejected rather than written as a no-op snooze
    the owner would believe had taken effect. Assign it to the python3
    process, not to a command earlier in the pipeline:
    `printf '%s\\n' <id> | SNOOZE_UNTIL=<YYYY-MM-DD> python3 snooze-orders.py`

Stdout on success: `{"snoozed": <int>, "not_snoozed": <int>,
"snooze_until": "<date>"}`. `snoozed` counts ids whose row was eligible and
updated; `not_snoozed` counts ids matching no eligible row (stale id, never
imported, or already terminal — diagnostic, not an error).

Unlike the Step 8 reader, this script does NOT tolerate a missing
`snooze_until` column: a reader degrading to "nothing is snoozed" is
harmless, but a writer silently dropping the owner's acknowledgement is
not. An un-migrated database fails loudly with the migration to deploy.

This is an ad-hoc tool invoked outside the nightly flow, in response to an
owner acknowledgement — not a Step in the sequential check-orders run.

Exit codes: 0 success, 1 IO/schema error, 2 bad or missing SNOOZE_UNTIL.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


def _resolve_snooze_until() -> str:
    """Read and validate SNOOZE_UNTIL, or exit 2 with an actionable message.

    Returns the normalized `YYYY-MM-DD` string to persist.
    """
    raw = os.environ.get("SNOOZE_UNTIL", "").strip()
    if not raw:
        sys.stderr.write(
            "snooze-orders: SNOOZE_UNTIL is required. Set it to the ISO date "
            "the snooze runs until (exclusive), assigning it to the python3 "
            "process so it survives the pipe, e.g. "
            "`printf '%s\\n' <id> | SNOOZE_UNTIL=<YYYY-MM-DD> "
            "python3 snooze-orders.py`.\n"
        )
        raise SystemExit(2)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        sys.stderr.write(
            f"snooze-orders: SNOOZE_UNTIL={raw!r} is not an ISO date. "
            f"Use YYYY-MM-DD, e.g. SNOOZE_UNTIL=2026-09-01.\n"
        )
        raise SystemExit(2) from None
    if parsed <= date.today():
        sys.stderr.write(
            f"snooze-orders: SNOOZE_UNTIL={raw} is not in the future "
            f"(today is {date.today().isoformat()}). A past or same-day "
            f"value would suppress nothing — pick a later date.\n"
        )
        raise SystemExit(2)
    return parsed.isoformat()


def main() -> int:
    snooze_until = _resolve_snooze_until()

    try:
        raw = sys.stdin.read()
    except OSError as exc:
        sys.stderr.write(
            f"snooze-orders: failed to read order ids from stdin: {exc}. "
            f"Pipe the ids to snooze, one per line, e.g. "
            f"`printf '%s\\n' <id1> <id2> | python3 snooze-orders.py`.\n"
        )
        return 1
    ids = [line.strip() for line in raw.splitlines() if line.strip()]
    if not ids:
        # No ids to snooze — degenerate but legal.
        json.dump(
            {"snoozed": 0, "not_snoozed": 0, "snooze_until": snooze_until},
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    snoozed = 0
    not_snoozed = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Single transaction so the snooze set is all-or-nothing per
        # invocation — a transient failure mid-batch leaves the table
        # untouched and the agent can safely re-run with the same ids.
        with conn:
            for order_id in ids:
                cur = conn.execute(
                    """
                    UPDATE orders
                       SET snooze_until = ?,
                           last_updated = ?
                     WHERE id = ?
                       AND status IN ('ordered', 'shipped')
                    """,
                    (snooze_until, now_iso, order_id),
                )
                # rowcount is 1 when the id existed AND was in a snoozable
                # status; 0 when the id is missing or already terminal.
                if cur.rowcount > 0:
                    snoozed += 1
                else:
                    not_snoozed += 1
        json.dump(
            {
                "snoozed": snoozed,
                "not_snoozed": not_snoozed,
                "snooze_until": snooze_until,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"snooze-orders: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is writable, and the orders "
            f"table carries the snooze_until column (added by the "
            f"orchestrator's state-018 migration, jbaruch/nanoclaw#917) — "
            f"deploy that migration if this run reports no such column.\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
