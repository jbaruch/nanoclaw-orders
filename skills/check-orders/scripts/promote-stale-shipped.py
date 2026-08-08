#!/usr/bin/env python3
"""Auto-promote stale shipped/ordered rows to assumed_delivered.

Step 7 of check-orders SKILL.md: some senders (e.g. Chewy Autoship)
never send a delivered email — the order arrives but the status in
the orders table stays `shipped` forever and Step 10's "Overdue
delivery" rule keeps firing for the full 30-day cutoff.

Eligibility (all three must hold):
  - status IN ('shipped', 'ordered') — only non-terminal statuses
  - expected_delivery NOT NULL AND parses as ISO date AND >= 10 days
    before today (real overdue, not slow shipping)
  - last_updated >= 10 days before now (no fresh email about this
    order in >= 10 days)

`expected_delivery` is sometimes a free-text string like "overnight"
rather than an ISO date. Per the original within-days.py contract, a
malformed date counts as eligible (it's been long enough that no
specific date is meaningful). Implemented here in Python rather than
a single SQL UPDATE because the malformed-date branch can't be
expressed with the SQLite `date()` builtin without a complex
fallback.

Stdout on success: a single JSON object summarising the pass:
    {"promoted": <int>, "ids": ["amazon-...", ...]}

Stderr on per-row issues (e.g. unparseable last_updated): warn line
prefixed with the order id, and the row is skipped.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")
DAYS_THRESHOLD = 10


def _expected_delivery_eligible(value) -> bool:
    """True when the value should count as overdue per Step 7.

    Defensive against non-string types (SQLite is type-permissive — a
    hand-edited row with an integer/numeric value would otherwise crash
    `.strip()` with AttributeError and abort the whole pass). NULL,
    empty strings, and non-string values all return False (ineligible)
    so the caller treats them like a missing expected_delivery.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    # Try parsing as ISO date (first 10 chars). Mirrors within-days.py.
    try:
        parsed = date.fromisoformat(stripped[:10])
    except ValueError:
        # Malformed date string ("overnight", "soon", etc.) — per the
        # original SKILL.md Step 7, these count as eligible because
        # it's been long enough that no specific date is meaningful.
        # The other two conditions (status + last_updated) still
        # gate, so this isn't a blanket promotion.
        return True
    return (date.today() - parsed).days >= DAYS_THRESHOLD


def _last_updated_eligible(value, order_id: str) -> bool:
    """True when last_updated is >= DAYS_THRESHOLD days ago.

    Defensive against NULL/non-string values: legacy rows pre-dating the
    NOT NULL constraint could still surface here if the schema is ever
    relaxed, and a hand-edited row with an integer/numeric column would
    otherwise crash `value.replace(...)` with AttributeError before the
    ValueError handler could catch it. Treat anything other than a
    non-empty string as "ineligible" — same outcome as a malformed
    ISO date — and skip with a diagnostic.
    """
    if not isinstance(value, str) or not value.strip():
        sys.stderr.write(
            f"promote-stale-shipped: order {order_id} has missing or "
            f"non-string last_updated={value!r}; skipping. To make this "
            f"row eligible for auto-promotion, set its last_updated to "
            f"an ISO-8601 timestamp at least 10 days in the past — "
            f"promotion requires the row to be stale, not fresh.\n"
        )
        return False
    try:
        # last_updated is ISO-8601 with timezone (e.g. "2026-04-02T00:00:00.000Z"
        # or "2026-04-02T00:00:00.000+00:00"). datetime.fromisoformat in 3.11+
        # accepts "Z" directly; for older, normalise.
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        sys.stderr.write(
            f"promote-stale-shipped: order {order_id} has unparseable "
            f"last_updated={value!r}; skipping\n"
        )
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    return delta.days >= DAYS_THRESHOLD


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    promoted_ids: list[str] = []
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """
            SELECT id, expected_delivery, last_updated
              FROM orders
             WHERE status IN ('shipped', 'ordered')
            """,
        )
        candidates = cur.fetchall()
        for order_id, expected_delivery, last_updated in candidates:
            if not _expected_delivery_eligible(expected_delivery):
                continue
            if not _last_updated_eligible(last_updated, order_id):
                continue
            conn.execute(
                """
                UPDATE orders
                   SET status = 'assumed_delivered',
                       flagged = 0,
                       flag_reason = NULL,
                       last_updated = ?
                 WHERE id = ?
                """,
                (now_iso, order_id),
            )
            promoted_ids.append(order_id)
        conn.commit()
        json.dump({"promoted": len(promoted_ids), "ids": promoted_ids}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"promote-stale-shipped: SQLite error against {DB_PATH}: "
            f"{exc}. Verify the database file exists, is writable, and "
            f"the orders table is present (created by the orchestrator's "
            f"state-001 migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
