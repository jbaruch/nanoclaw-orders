#!/usr/bin/env python3
"""Apply Step 8 flagging rules to all non-excluded orders.

Step 8 of check-orders SKILL.md — set `flagged=1` and `flag_reason=...`
for orders that match an anomaly rule, AND unflag rows that are past
their cutoff. Excluded orders (Step 6) must already have been
unflagged by the time this runs; this pass intentionally does NOT
look at exclusion rules — it just applies the anomaly conditions to
whatever is currently in the table.

Anomaly rules. Where a row could match multiple rules (e.g. a
`shipped` row with `amount > 200` AND an overdue `expected_delivery`),
the first match wins per the order below — the large-purchase rule
takes precedence over the overdue-delivery rule because the dollar
amount is the more user-actionable signal:

  | match                                      | flag_reason             | cutoff                |
  |--------------------------------------------|-------------------------|-----------------------|
  | status=cancelled                           | "Order cancelled"       | 14d from order_date   |
  | status=refunded                            | "Refund/return"         | 14d from order_date   |
  | non-delivered, amount>200                  | "Large purchase: $X.YY" | none                  |
  | delivered, amount>200                      | "Large purchase: $X.YY" | 14d from order_date   |
  | shipped|ordered, expected_delivery >2d ago | "Overdue delivery"      | 30d from exp_delivery |

Stdout on success: a single JSON object summarising the pass:
    {"flagged": <int>, "unflagged": <int>, "ids_flagged": [...], "ids_unflagged": [...]}

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")
LARGE_PURCHASE_THRESHOLD = 200.0


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


def _classify(row: dict) -> tuple[bool, str | None]:
    """Return (should_flag, flag_reason) for a single order row.

    Implements the anomaly rules above. Order matters when multiple
    rules can match the same row (e.g. shipped + amount > 200 + overdue
    expected_delivery). First-match-wins: the dollar-amount rule is
    listed before the overdue rule because the user-actionable signal
    is "money tied up" rather than "delivery is late".
    """
    status = row["status"]
    amount = row["amount"]
    order_date = row["order_date"]
    expected_delivery = row["expected_delivery"]

    if status == "cancelled" and _within_days(order_date, 14):
        return True, "Order cancelled"
    if status == "refunded" and _within_days(order_date, 14):
        return True, "Refund/return"
    # `amount` lands here as whatever `sqlite3.Row` returns from the
    # REAL column, which in practice is `None` or a number — but
    # SQLite is type-permissive (a string snuck into a REAL column on
    # a hand-edited row would surface here too) and Python would
    # raise TypeError on `amount > 200` if it's not numeric. Narrow
    # to int/float (excluding bool, which is technically int) before
    # comparing; anything else logs and skips this rule for the row.
    if (
        amount is not None
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and amount > LARGE_PURCHASE_THRESHOLD
    ):
        if status != "delivered":
            return True, f"Large purchase: ${amount:.2f}"
        if status == "delivered" and _within_days(order_date, 14):
            return True, f"Large purchase: ${amount:.2f}"
    elif amount is not None and not isinstance(amount, (int, float)):
        sys.stderr.write(
            f"flag-anomalies: order {row['id']} has non-numeric "
            f"amount={amount!r}; skipping the large-purchase rule for "
            f"this row\n"
        )
    if (
        status in ("shipped", "ordered")
        and _expected_delivery_overdue(expected_delivery)
        and _expected_delivery_within_30_days(expected_delivery)
    ):
        return True, "Overdue delivery"
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
            "SELECT id, status, amount, order_date, expected_delivery, "
            "flagged, flag_reason FROM orders"
        ).fetchall()
        for row in rows:
            if row["id"] in excluded_ids:
                continue
            should_flag, reason = _classify(dict(row))
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
