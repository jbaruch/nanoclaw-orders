#!/usr/bin/env python3
"""Apply Step 9 flagging rules to all non-excluded orders.

Step 9 of check-orders SKILL.md — set `flagged=1` and `flag_reason=...`
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
  |   AND the logical order not superseded     |                            |                     |
  | status=ordered, id in STUCK_IDS            | "Ordered, not yet shipped" | supplied by caller  |

An unlapsed `snooze_until` (`jbaruch/nanoclaw#917`) suppresses EVERY
rule above for that row, and unflags it if it was already flagged: the
owner asked to stop hearing about the order, not to stop hearing about
one particular reason for it. Written by `snooze-orders.py`.

Superseded rows (`jbaruch/nanoclaw-orders#68`): one logical order can
land on two rows. The row id is `{source}-{order_date}-{sha1(description)[:8]}`
(`compute-order-id.py`), so a shipment email whose description differs from
the confirmation's by one character ("1 Essentials item" vs "Essentials
item") creates a second row instead of updating the first. The stale
`ordered` row keeps the `expected_delivery` the confirmation carried and
flags "Overdue delivery" on its own for days after the order shipped — the
symptom `#68` reports. The Overdue rule therefore skips a row whose logical
order already holds a further-along row dated no earlier: a
`shipped`/`delivered`/`assumed_delivered` sibling for an `ordered` row, a
`delivered`/`assumed_delivered` sibling for a `shipped` one. Logical-order
identity is the `(source, order_number)` key `compute-stuck-orders.py` pairs
on, so both scripts reconcile split rows the same way. Progression must be
strict, so two `shipped` rows of one order never silence each other and an
overdue delivery can never go quiet through a same-status duplicate. A
blank `order_number` cannot be paired and keeps the current behaviour.

Stuck-order rule (`jbaruch/nanoclaw-orders#55`): the primary signal the
owner wants is "placed weeks ago, never shipped". `compute-stuck-orders.py`
(Step 8) decides which `ordered` rows are stuck — a deterministic join on
the persisted `order_number` column pairs an order's confirmation and
shipment rows — and passes the surviving stuck ids here via the STUCK_IDS
env var (comma-separated, same shape as EXCLUDED_IDS). This script only
writes the flag for ids it is handed.

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

# Canonical extended calendar date — the only `snooze_until` shape
# `snooze-orders.py` writes and the only one honoured here. Kept
# identical to `compute-stuck-orders.py`'s copy.
_CANONICAL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

# Order-lifecycle progression, low to high, for the supersession test in
# `_superseded_ids`. `delivered` and `assumed_delivered` share the top rank:
# both mean the order arrived, one reported by the merchant and one by the
# owner (`ack-orders.py`). A status absent here never supersedes and is
# never superseded.
_STATUS_RANK = {
    "ordered": 0,
    "shipped": 1,
    "delivered": 2,
    "assumed_delivered": 2,
}


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


def _order_key(source, order_number):
    """(source, order_number) logical-order key, or None when the row has
    no usable order number (NULL/blank) and so cannot be paired.

    Identical to `compute-stuck-orders.py`'s copy — duplicated rather than
    imported for the same reason `_snooze_open` is (standalone executables,
    no shared module), and surface-synced with it.
    """
    if isinstance(order_number, str) and order_number.strip():
        return (source, order_number.strip())
    return None


def _order_day(value):
    """Parse a row's `order_date` to a date, or None when unusable.

    Slices the leading 10 characters like `_within_days` does — `order_date`
    legitimately carries a full ISO timestamp. A non-string or malformed
    value yields None, which keeps the row out of every supersession
    comparison: a date that cannot be read must never be the reason an
    overdue order goes quiet.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _superseded_ids(rows) -> set:
    """Ids whose logical order already holds a further-along, no-earlier row.

    Progression rank is `ordered` < `shipped` < `delivered`/
    `assumed_delivered` — the two terminal statuses share the top rank
    (`ack-orders.py` writes `assumed_delivered` for a delivery the owner
    confirmed out of band). A row is superseded when a sibling on the same
    `(source, order_number)` key carries a STRICTLY higher rank and an
    `order_date` no earlier than its own.

    Strictness is what keeps the suppression safe: same-rank siblings never
    supersede each other, so a pair of `shipped` rows for one order cannot
    silence both halves of a genuinely overdue delivery. The no-earlier date
    test keeps an old shipment from vouching for a later re-order that reuses
    the merchant's order number.

    Statuses outside the rank map (`cancelled`, `refunded`, `unknown`) take
    no part on either side — they are neither superseded nor superseding.
    """
    latest_by_key: dict = {}
    for row in rows:
        rank = _STATUS_RANK.get(row["status"])
        key = _order_key(row["source"], row["order_number"])
        day = _order_day(row["order_date"])
        if rank is None or key is None or day is None:
            continue
        by_rank = latest_by_key.setdefault(key, {})
        if rank not in by_rank or day > by_rank[rank]:
            by_rank[rank] = day

    superseded = set()
    for row in rows:
        rank = _STATUS_RANK.get(row["status"])
        key = _order_key(row["source"], row["order_number"])
        day = _order_day(row["order_date"])
        if rank is None or key is None or day is None:
            continue
        by_rank = latest_by_key.get(key, {})
        if any(other > rank and latest >= day for other, latest in by_rank.items()):
            superseded.add(row["id"])
    return superseded


def _snooze_open(snooze_until) -> bool:
    """True iff the row carries a snooze window that has not yet lapsed.

    Mirrors `compute-stuck-orders.py`'s reader exactly — canonical
    `YYYY-MM-DD` only, suppression while `today < snooze_until`, and a
    malformed value degrading to "not snoozed" so a bad marker can never
    hide a real alert. Duplicated rather than imported because these
    scripts are standalone executables with no shared module (same
    pattern as `_within_days` and `within-days.py`); the two copies are
    surface-synced.
    """
    if not isinstance(snooze_until, str):
        return False
    value = snooze_until.strip()
    if not _CANONICAL_DATE.match(value):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return date.today() < parsed


def _has_snooze_column(conn) -> bool:
    """True iff the `orders` table carries `snooze_until` (`state-018`).

    Absent means "nothing is snoozed", never an error — the plugin runs
    against databases the orchestrator has not migrated yet
    (`coding-policy: stateful-artifacts` reader discipline).
    """
    cols = conn.execute("PRAGMA table_info(orders)").fetchall()
    return any(col["name"] == "snooze_until" for col in cols)


def _classify(row: dict, stuck_ids: set, superseded_ids: set) -> tuple[bool, str | None]:
    """Return (should_flag, flag_reason) for a single order row.

    `stuck_ids` is the set of `ordered`-row ids `compute-stuck-orders.py`
    (Step 8) determined are stuck (aged, with no matching shipment). This
    script trusts that structured list rather than re-deriving it.

    `superseded_ids` is `_superseded_ids`'s verdict over the whole table:
    rows whose logical order has already moved further along. Only the
    Overdue rule consults it — a cancellation or refund is that row's own
    news, and the stuck rule already applied the same pairing upstream.

    Implements the anomaly rules above. First-match-wins in table order:
    a cancellation/refund outranks an overdue signal, and a concrete
    overdue `expected_delivery` outranks the supplied stuck signal.

    An open snooze window outranks all of them. Suppressing here rather
    than in Step 8 alone is what makes the marker mean what the owner
    means by it — "stop alerting on this row until <date>", not "stop
    alerting only when the stuck rule happens to be the one that fires".
    Step 8 reaches only `ordered` rows via the stuck rule, so without
    this an `ordered` row with an overdue `expected_delivery` still flags
    on the higher-priority rule, and a snoozed `shipped` row — which
    `snooze-orders.py` accepts — never enters Step 8 at all.
    """
    if _snooze_open(row["snooze_until"]):
        return False, None

    status = row["status"]
    order_date = row["order_date"]
    expected_delivery = row["expected_delivery"]

    if status == "cancelled" and _within_days(order_date, 14):
        return True, "Order cancelled"
    if status == "refunded" and _within_days(order_date, 14):
        return True, "Refund/return"
    if (
        status in ("shipped", "ordered")
        and row["id"] not in superseded_ids
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

        # Stuck-order ids from compute-stuck-orders.py (Step 8),
        # comma-separated (same shape as EXCLUDED_IDS). Empty = none stuck.
        stuck_ids_raw = os.environ.get("STUCK_IDS", "")
        stuck_ids = {s.strip() for s in stuck_ids_raw.split(",") if s.strip()}

        # Select `snooze_until` only when state-018 has been applied; on an
        # un-migrated database the literal NULL keeps the row shape stable
        # so `_classify` needs no second code path.
        snooze_select = "snooze_until" if _has_snooze_column(conn) else "NULL AS snooze_until"
        rows = conn.execute(
            "SELECT id, status, source, order_date, order_number, "
            f"expected_delivery, flagged, flag_reason, {snooze_select} FROM orders"
        ).fetchall()

        # Built over every row, excluded ones included: an excluded shipment
        # still proves its `ordered` sibling shipped, exactly as Step 8 builds
        # its shipped keys over the whole table.
        superseded_ids = _superseded_ids(rows)

        for row in rows:
            if row["id"] in excluded_ids:
                continue
            should_flag, reason = _classify(dict(row), stuck_ids, superseded_ids)
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
            f"state-001 migration, with order_number added by state-017).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
