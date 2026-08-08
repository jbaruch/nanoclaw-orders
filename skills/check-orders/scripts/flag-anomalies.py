#!/usr/bin/env python3
"""Apply Step 8 flagging rules to all non-excluded orders.

Step 8 of check-orders SKILL.md — set `flagged=1` and `flag_reason=...`
for orders that match an anomaly rule, AND unflag rows that are past
their cutoff. Excluded orders (Step 6) must already have been
unflagged by the time this runs; this pass intentionally does NOT
look at exclusion rules — it just applies the anomaly conditions to
whatever is currently in the table.

Anomaly rules. Where a row could match multiple rules (e.g. an
`ordered` row that is both overdue on a concrete `expected_delivery`
AND long-stuck), the first match wins per the order below:

  | match                                      | flag_reason                | cutoff              |
  |--------------------------------------------|----------------------------|---------------------|
  | status=cancelled                           | "Order cancelled"          | 14d from order_date |
  | status=refunded                            | "Refund/return"            | 14d from order_date |
  | shipped|ordered, expected_delivery >2d ago | "Overdue delivery"         | 30d from exp_deliv  |
  | status=ordered, no shipped sibling, aged   | "Ordered, not yet shipped" | 90d from order_date |

Stuck-order rule (`jbaruch/nanoclaw-orders#55`): the primary signal the
owner wants is "placed weeks ago, never shipped". An `ordered` row whose
`order_date` is between STUCK_ORDER_MIN_DAYS and STUCK_ORDER_MAX_DAYS old,
with no `shipped`/`delivered`/`assumed_delivered` row for the same logical
order, is flagged. Logical-order identity is the order number extracted
from `description` (see `_order_number`); a row with no extractable order
number stands alone (it cannot be paired with a shipment, so it flags on
age alone). This is a heuristic pairing — true row-level dedup by order
number is tracked separately.

The "Large purchase" rule was removed (`#55`): it flagged self-made
purchases the owner already knew about (concert tickets, a laptop) with no
action attached, and it was the reason one logical order surfaced twice —
once per email — since both the confirmation and the shipment row cleared
the dollar threshold. Rows previously carrying a `Large purchase: $...`
reason are unflagged by the past-cutoff branch on the next pass.

Stdout on success: a single JSON object summarising the pass:
    {"flagged": <int>, "unflagged": <int>, "ids_flagged": [...], "ids_unflagged": [...]}

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import date

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

# Stuck-order age window (days from order_date). Below MIN, a slow ship is
# normal and not yet a signal; above MAX, the alert has aged out to keep the
# channel signal-only (same aging-out philosophy as the other cutoffs).
STUCK_ORDER_MIN_DAYS = 7
STUCK_ORDER_MAX_DAYS = 90

# Statuses that prove a logical order left the `ordered` stage. An `ordered`
# row paired (by order number) with any of these is not stuck.
_SHIPPED_STATUSES = ("shipped", "delivered", "assumed_delivered")

# An order-number token: up to 4 leading letters then 5+ digits (W1584689498,
# US5848051, 170910). The 5-digit floor keeps dates (2026), short SKUs (17L),
# and dollar amounts (512) from being mistaken for order numbers. Descriptions
# are sender-controlled subject fragments, so this is a best-effort pairing key
# — see the module docstring on heuristic pairing.
_ORDER_NUM_RE = re.compile(r"[A-Za-z]{0,4}\d{5,}")


def _within_days(value, days: int) -> bool:
    """True iff value parses as ISO date AND is within last N days.

    Type-guards against SQLite's permissiveness — a hand-edited row
    with a non-string value in `order_date` or `expected_delivery`
    would otherwise crash `value.strip()` with AttributeError and
    abort the whole flagging pass. Anything that isn't a non-empty
    string returns False (ineligible) — same outcome as a malformed
    ISO date.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return False
    delta = (date.today() - parsed).days
    return 0 <= delta <= days


def _older_than_days(value, days: int) -> bool:
    """True iff value parses as ISO date AND is strictly more than N days ago.

    Same type-guarding rationale as `_within_days`.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return False
    return (date.today() - parsed).days > days


def _expected_delivery_overdue(value) -> bool:
    """True iff value parses as ISO date AND is >2 days before today.

    Same type-guarding rationale as `_within_days` above.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return False
    return (date.today() - parsed).days > 2


def _expected_delivery_within_30_days(value: str | None) -> bool:
    return _within_days(value, 30)


def _order_number(description) -> str | None:
    """Extract an order-number token from a description, or None.

    Type-guards against non-string values (SQLite permissiveness, same
    rationale as `_within_days`). Returns the first matching token
    upper-cased so ordered/shipped emails of the same order — which
    quote the same order number in their subjects — group together.
    """
    if not isinstance(description, str):
        return None
    match = _ORDER_NUM_RE.search(description)
    return match.group(0).upper() if match else None


def _logical_key(row: dict) -> tuple[str, str] | None:
    """(source, order_number) identity for a row, or None when no order
    number is extractable (the row cannot be paired with a shipment)."""
    number = _order_number(row["description"])
    if number is None:
        return None
    return (row["source"], number)


def _classify(row: dict, shipped_keys: set) -> tuple[bool, str | None]:
    """Return (should_flag, flag_reason) for a single order row.

    `shipped_keys` is the set of logical-order keys (see `_logical_key`)
    that have at least one shipped/delivered row — an `ordered` row whose
    key is in this set has shipped and is not stuck.

    Implements the anomaly rules above. First-match-wins in table order:
    a cancellation/refund outranks an overdue signal, and a concrete
    overdue `expected_delivery` outranks the age-only stuck signal.
    """
    status = row["status"]
    order_date = row["order_date"]
    expected_delivery = row["expected_delivery"]

    if status == "cancelled" and _within_days(order_date, 14):
        return True, "Order cancelled"
    if status == "refunded" and _within_days(order_date, 14):
        return True, "Refund/return"
    if (
        status in ("shipped", "ordered")
        and _expected_delivery_overdue(expected_delivery)
        and _expected_delivery_within_30_days(expected_delivery)
    ):
        return True, "Overdue delivery"
    if (
        status == "ordered"
        and _older_than_days(order_date, STUCK_ORDER_MIN_DAYS)
        and _within_days(order_date, STUCK_ORDER_MAX_DAYS)
        and _logical_key(row) not in shipped_keys
    ):
        return True, "Ordered, not yet shipped"
    return False, None


def main() -> int:
    flagged_ids: list[str] = []
    unflagged_ids: list[str] = []
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Walk every non-excluded row. "Non-excluded" is encoded
        # implicitly: Step 6 already set excluded rows to flagged=0,
        # flag_reason=NULL, and the rules below would re-flag them
        # if their conditions matched. To prevent that, the SKILL
        # tells the agent to track excluded ids in memory and pass
        # them via the EXCLUDED_IDS env var (comma-separated). Empty
        # = no exclusions to honour.
        excluded_ids_raw = os.environ.get("EXCLUDED_IDS", "")
        excluded_ids = {s.strip() for s in excluded_ids_raw.split(",") if s.strip()}

        rows = conn.execute(
            "SELECT id, status, order_date, expected_delivery, "
            "source, description, flagged, flag_reason FROM orders"
        ).fetchall()

        # Precompute logical-order keys that have shipped/delivered. Built
        # over ALL rows (excluded ones included): an excluded shipment still
        # proves its `ordered` sibling is not stuck.
        shipped_keys = set()
        for row in rows:
            if row["status"] in _SHIPPED_STATUSES:
                key = _logical_key(dict(row))
                if key is not None:
                    shipped_keys.add(key)

        for row in rows:
            if row["id"] in excluded_ids:
                continue
            should_flag, reason = _classify(dict(row), shipped_keys)
            current_flagged = bool(row["flagged"])
            if should_flag and (not current_flagged or row["flag_reason"] != reason):
                conn.execute(
                    "UPDATE orders SET flagged = 1, flag_reason = ? WHERE id = ?",
                    (reason, row["id"]),
                )
                flagged_ids.append(row["id"])
            elif not should_flag and current_flagged:
                conn.execute(
                    "UPDATE orders SET flagged = 0, flag_reason = NULL WHERE id = ?",
                    (row["id"],),
                )
                unflagged_ids.append(row["id"])
        conn.commit()
        json.dump(
            {
                "flagged": len(flagged_ids),
                "unflagged": len(unflagged_ids),
                "ids_flagged": flagged_ids,
                "ids_unflagged": unflagged_ids,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"flag-anomalies: SQLite error against {DB_PATH}: {exc}. "
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
