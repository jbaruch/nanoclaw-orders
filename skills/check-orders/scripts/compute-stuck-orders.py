#!/usr/bin/env python3
"""Compute the ids of orders stuck in `ordered` with no shipment.

Step 8 of check-orders SKILL.md. Deterministic stuck-order detection
(`jbaruch/nanoclaw-orders#55`): an `ordered` row whose `order_date` falls
in the stuck window — at least STUCK_ORDER_MIN_DAYS and at most
STUCK_ORDER_MAX_DAYS before today, both bounds inclusive — is stuck unless
its logical order has a shipment row. Logical-order identity is the
persisted `(source, order_number)` key; a row whose `order_number` is NULL
cannot be paired to a shipment, so it is stuck on age alone.

Pairing is a deterministic join on the stored `order_number` column
(populated at ingestion, `#58`), so it lives in this script — the earlier
agent-pairing step is gone. The confirmation and shipment emails of one
order carry the same order number, so an order that shipped is not counted
as stuck.

Below STUCK_ORDER_MIN_DAYS a slow ship is normal, not yet a signal; above
STUCK_ORDER_MAX_DAYS the alert ages out (channel stays signal-only, same
philosophy as the flag-anomalies cutoffs).

Two further suppressions keep rows out of the pool (`#63`):

  - **Never-ship merchants** (NEVER_SHIP_MERCHANTS): crowdfunding,
    subscription, and digital sources emit no shipment email ever, so
    "ordered with no shipment" is their steady state, not an anomaly.
    Without this they flag for the whole `[MIN, MAX]` window before the
    age ceiling drains them. Matching mirrors `apply-exclusions.py`'s
    precedence: the persisted `merchant` column is authoritative when
    present, and only a NULL/blank `merchant` (legacy rows predating
    `state-017`) on an unclassified `source` falls through to a
    `description` substring match.
  - **Open snooze window** (`snooze_until`, `jbaruch/nanoclaw#917`):
    an order the owner has acknowledged as *genuinely still not
    shipped*. `ack-orders.py`'s `assumed_delivered` transition would
    record a delivery that never happened, so the snooze marker
    suppresses re-flagging while leaving `status = 'ordered'` honest.
    Suppression holds while `today < snooze_until`.

`snooze_until` is read only when the column exists — the tile keeps
working against a database that has not applied `state-018` yet, per
`coding-policy: stateful-artifacts` cross-pipeline reader discipline.
An absent column means "nothing is snoozed", never an error.

Stdout on success: `{"stuck_ids": ["amazon-...", ...]}` (ascending id
order). The list feeds `flag-anomalies.py`'s STUCK_IDS at Step 9.

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

# Merchants that never emit a shipment email, so an `ordered` row from one
# is never evidence of a stuck order (`#63`). Curated and fully enumerable
# per `coding-policy: script-delegation` "The Regex Trap" — substrings, not
# fuzzy matching. Extend this tuple when a new never-ship merchant shows
# up in the backlog; there is deliberately no pattern-inference path.
#
# Kickstarter and Indiegogo pledges ship (if ever) months to years later
# and outside the email trail entirely. Patreon and Substack are recurring
# subscriptions with no physical fulfilment at all.
NEVER_SHIP_MERCHANTS = (
    "kickstarter",
    "indiegogo",
    "patreon",
    "substack",
)

# `classify-order.py`'s fallback source for a domain it does not map to
# amazon / shopify / shop — where every never-ship merchant lands. Gates
# the description fallback in `_never_ships` so a known-shipping source
# is never suppressed by description text alone.
_UNCLASSIFIED_SOURCE = "other"


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
    return STUCK_ORDER_MIN_DAYS <= delta <= STUCK_ORDER_MAX_DAYS


def _order_key(source, order_number):
    """(source, order_number) logical-order key, or None when the row has
    no usable order number (NULL/blank) and so cannot be paired."""
    if isinstance(order_number, str) and order_number.strip():
        return (source, order_number.strip())
    return None


def _never_ships(source, merchant, description) -> bool:
    """True iff the row belongs to a merchant that never emits a shipment
    email, so "ordered with no shipment" is its steady state.

    Precedence mirrors `apply-exclusions.py`: a populated `merchant` is
    authoritative and the description is NOT consulted, so a row whose
    merchant is known-shipping stays eligible even if its description
    happens to mention a never-ship name ("bought on Amazon with my
    Kickstarter refund").

    A NULL/blank `merchant` — a legacy row predating `state-017` — falls
    through to the description, but only for `source = 'other'`. Every
    never-ship merchant classifies there (`classify-order.py` maps only
    amazon / shopify / shop domains to their own source), so a row from a
    known-shipping source can never be suppressed by description text
    alone. Without that gate a NULL-merchant Amazon row whose description
    mentions a pledge would drop out of the pool silently.
    """
    if isinstance(merchant, str) and merchant.strip():
        lowered = merchant.lower()
        return any(name in lowered for name in NEVER_SHIP_MERCHANTS)
    if source == _UNCLASSIFIED_SOURCE and isinstance(description, str):
        lowered = description.lower()
        return any(name in lowered for name in NEVER_SHIP_MERCHANTS)
    return False


def _snooze_open(snooze_until) -> bool:
    """True iff the row carries a snooze window that has not yet lapsed.

    Suppression holds while `today < snooze_until`, so the boundary day
    itself re-flags — a snooze "until 2026-09-01" is over ON 2026-09-01.
    Type-guards the same way `_aged_candidate` does: a non-string or
    malformed value is treated as "not snoozed" rather than crashing the
    nightly run, since a bad marker must never suppress a real alert.
    """
    if not isinstance(snooze_until, str) or not snooze_until.strip():
        return False
    try:
        parsed = date.fromisoformat(snooze_until.strip()[:10])
    except ValueError:
        return False
    return date.today() < parsed


def _has_snooze_column(conn) -> bool:
    """True iff the `orders` table carries `snooze_until` (`state-018`).

    The tile may run against a database the orchestrator has not migrated
    yet, so the column's absence is a normal state meaning "nothing is
    snoozed" — never an error (`stateful-artifacts` reader discipline).
    """
    cols = conn.execute("PRAGMA table_info(orders)").fetchall()
    return any(col["name"] == "snooze_until" for col in cols)


def main() -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Select `snooze_until` only when state-018 has been applied; on an
        # un-migrated database the literal NULL keeps the row shape stable
        # so the loop below needs no second code path.
        snooze_select = "snooze_until" if _has_snooze_column(conn) else "NULL AS snooze_until"
        rows = conn.execute(
            "SELECT id, status, source, order_date, order_number, merchant, "
            f"description, {snooze_select} FROM orders"
        ).fetchall()

        # Logical-order keys proven shipped. Built over all rows so an
        # excluded shipment still proves its `ordered` sibling shipped.
        shipped_keys = set()
        for row in rows:
            if row["status"] in _SHIPPED_STATUSES:
                key = _order_key(row["source"], row["order_number"])
                if key is not None:
                    shipped_keys.add(key)

        stuck_ids = []
        for row in rows:
            if row["status"] != "ordered" or not _aged_candidate(row["order_date"]):
                continue
            if _never_ships(row["source"], row["merchant"], row["description"]):
                continue
            if _snooze_open(row["snooze_until"]):
                continue
            key = _order_key(row["source"], row["order_number"])
            if key is None or key not in shipped_keys:
                stuck_ids.append(row["id"])

        json.dump({"stuck_ids": sorted(stuck_ids)}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"compute-stuck-orders: SQLite error against {DB_PATH}: {exc}. "
            f"Verify the database file exists, is readable, and the orders "
            f"table is present (created by the orchestrator's state-001 "
            f"migration, with order_number added by state-017).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
