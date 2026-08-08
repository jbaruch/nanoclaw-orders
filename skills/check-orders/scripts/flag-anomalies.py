#!/usr/bin/env python3
"""Apply Step 10 flagging rules to all non-excluded orders.

Step 10 of check-orders SKILL.md — set `flagged=1` and `flag_reason=...`
for orders that match an anomaly rule, AND unflag rows that are past
their cutoff. Excluded orders (Step 6) must already have been
unflagged by the time this runs; this pass intentionally does NOT
look at exclusion rules — it just applies the anomaly conditions to
whatever is currently in the table.

Anomaly rules. Where a row could match multiple rules (e.g. an
`ordered` row that is both overdue on a concrete `expected_delivery`
AND supplied as stuck), the first match wins per the order below:

  | match                                      | flag_reason                | cutoff              |
  |--------------------------------------------|----------------------------|---------------------|
  | status=cancelled                           | "Order cancelled"          | 14d from order_date |
  | status=refunded                            | "Refund/return"            | 14d from order_date |
  | shipped|ordered, expected_delivery >2d ago | "Overdue delivery"         | 30d from exp_deliv  |
  | status=ordered, id in STUCK_IDS            | "Ordered, not yet shipped" | supplied by caller  |

Stuck-order rule (`jbaruch/nanoclaw-orders#55`): the primary signal the
owner wants is "placed weeks ago, never shipped". Deciding which orders
are stuck needs a candidate row paired against shipment rows by an order
number written in sender-controlled subject text — that pairing is
reasoning, not scripting (`jbaruch/coding-policy: script-delegation`, the
Regex Trap), so it happens in the agent. The deterministic halves live in
scripts: `list-stuck-candidates.py` (Step 8) selects the aged `ordered`
rows and shipment rows; the agent pairs them (Step 9) and passes the
surviving stuck ids here via the STUCK_IDS env var (comma-separated,
same shape as EXCLUDED_IDS). This script only writes the flag for ids it
is handed — it never parses a description.

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
import sqlite3
import sys
from datetime import date

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


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


def _classify(row: dict, stuck_ids: set) -> tuple[bool, str | None]:
    """Return (should_flag, flag_reason) for a single order row.

    `stuck_ids` is the set of `ordered`-row ids the agent's Step 9 pairing
    determined are stuck (aged, with no matching shipment). This script
    trusts that structured list rather than re-deriving order identity
    from free text.

    Implements the anomaly rules above. First-match-wins in table order:
    a cancellation/refund outranks an overdue signal, and a concrete
    overdue `expected_delivery` outranks the supplied stuck signal.
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
    if status == "ordered" and row["id"] in stuck_ids:
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

        # Stuck-order ids the agent paired in Step 9 (comma-separated,
        # same shape as EXCLUDED_IDS). Empty = nothing stuck this pass.
        stuck_ids_raw = os.environ.get("STUCK_IDS", "")
        stuck_ids = {s.strip() for s in stuck_ids_raw.split(",") if s.strip()}

        rows = conn.execute(
            "SELECT id, status, order_date, expected_delivery, flagged, flag_reason FROM orders"
        ).fetchall()

        for row in rows:
            if row["id"] in excluded_ids:
                continue
            should_flag, reason = _classify(dict(row), stuck_ids)
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
