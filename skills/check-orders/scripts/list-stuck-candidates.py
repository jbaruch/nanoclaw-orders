#!/usr/bin/env python3
"""List aged `ordered` rows and shipment rows for stuck-order pairing.

Step 8 of check-orders SKILL.md. This script does the DETERMINISTIC half
of stuck-order detection (`jbaruch/nanoclaw-orders#55`): it selects the
`ordered` rows old enough to be candidates — `order_date` strictly more
than STUCK_ORDER_MIN_DAYS and at most STUCK_ORDER_MAX_DAYS before today —
and every shipment row (status in shipped/delivered/assumed_delivered).

It does NOT decide which candidates are stuck. Pairing a candidate to a
shipment means matching an order number written in sender-controlled
subject text, which is reasoning, not scripting (`jbaruch/coding-policy:
script-delegation`, the Regex Trap). The agent pairs them in Step 9 and
passes the surviving stuck ids to `flag-anomalies.py` as STUCK_IDS.

Below STUCK_ORDER_MIN_DAYS a slow ship is normal and not yet a signal;
above STUCK_ORDER_MAX_DAYS the alert has aged out (channel stays
signal-only, same aging-out philosophy as the flag-anomalies cutoffs).

Stdout on success: a single JSON object:
    {
      "candidates": [{"id": "...", "source": "...", "description": "..."}, ...],
      "shipments":  [{"id": "...", "source": "...", "description": "..."}, ...]
    }
`candidates` are the aged `ordered` rows; `shipments` are rows whose status
proves an order left the ordered stage. Descriptions are the sanitized
subject fragments already stored in the table (never raw Gmail).

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
    return STUCK_ORDER_MIN_DAYS < delta <= STUCK_ORDER_MAX_DAYS


def _project(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "source": row["source"], "description": row["description"]}


def main() -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, source, description, order_date FROM orders"
        ).fetchall()
        candidates: list[dict] = []
        shipments: list[dict] = []
        for row in rows:
            if row["status"] == "ordered" and _aged_candidate(row["order_date"]):
                candidates.append(_project(row))
            elif row["status"] in _SHIPPED_STATUSES:
                shipments.append(_project(row))
        json.dump({"candidates": candidates, "shipments": shipments}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"list-stuck-candidates: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is readable, and the orders "
            f"table is present (created by the orchestrator's state-001 "
            f"migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
