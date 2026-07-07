#!/usr/bin/env python3
"""Apply the user-preference exclusion table to the orders table.

Step 6 of check-orders SKILL.md. Owns the exclusion rules end-to-end
(per `coding-policy: script-delegation`): queries the orders table,
parses `to_address` with `email.utils.getaddresses`, applies the
EXCLUSIONS table below, unflags every match in one transaction, and
emits the excluded id list Step 8 passes to flag-anomalies.py via
EXCLUDED_IDS. The agent never re-implements address parsing or
matching rules in prose.

EXCLUSIONS is the runtime-authoritative mirror of the "Do NOT flag
these" list in `/workspace/trusted/user_preferences.md` — when that
list changes, update EXCLUSIONS in the same change.

Matching rules (implemented in `_matches`):
  - `source` must equal the rule's source exactly
  - `to_address` is parsed with `email.utils.getaddresses`, so
    display-name wrapping (`"Name" <addr>`) and comma-separated
    multi-recipient headers both resolve to bare addresses; a row
    matches when ANY recipient equals a rule address,
    case-insensitively on the full address
  - when `to_address` yields at least one recipient and none match,
    the row does NOT match — a parseable address is authoritative,
    and the description fallback must not overrule it (an Amazon
    order addressed to someone else stays flaggable even if its
    description mentions the excluded person)
  - ONLY rows with NULL/empty/unparseable `to_address` (historical
    rows predating the column) fall through to the description
    fallback: case-insensitive substring match against the rule's
    `description_substrings`

Stdout on success (ids in ascending `id` order via SQL `ORDER BY`):
    {"excluded_ids": [...], "excluded_ids_csv": "id1,id2",
     "matched": <int>, "unflagged": <int>}

Side effect: EVERY matched row is reset to `flagged = 0`,
`flag_reason = NULL` — including already-unflagged rows carrying a
stale `flag_reason` (idempotent normalization). `matched` counts rows
matching an exclusion rule (all appear in `excluded_ids`);
`unflagged` counts the subset that had `flagged = 1` before the pass.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from email.utils import getaddresses

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

# Runtime-authoritative exclusion table — mirror of the "Do NOT flag
# these" list in /workspace/trusted/user_preferences.md.
EXCLUSIONS = (
    {
        # Amazon noise via the family member's address: orders placed
        # for Amir on the shared account are his, not Baruch's.
        "name": "amazon-amir-family-address",
        "source": "amazon",
        "addresses": frozenset({"amir@sadogursky.com"}),
        "description_substrings": ("amir",),
    },
)


def _is_mailbox(addr: str) -> bool:
    """True when the token has an @-separated non-empty local and domain
    part. `getaddresses` returns free-text tokens (e.g. "Amir") as
    non-empty "addresses"; without this filter such rows would count as
    parseable and skip the description fallback."""
    local, sep, domain = addr.partition("@")
    return bool(sep and local and domain)


def _recipients(to_address) -> list[str]:
    """Parse a raw To: header into lowercased bare mailbox addresses.

    `getaddresses` handles display-name wrapping and comma-separated
    multi-recipient headers; tokens that are not @-shaped mailboxes
    (free-text historical values) are discarded so those rows fall
    through to the description fallback. Non-string / empty values
    (historical rows with NULL to_address) return an empty list.
    """
    if not isinstance(to_address, str) or not to_address.strip():
        return []
    return [addr.lower() for _name, addr in getaddresses([to_address]) if _is_mailbox(addr)]


def _matches(rule: dict, source, description, to_address) -> bool:
    if source != rule["source"]:
        return False
    recipients = _recipients(to_address)
    if recipients:
        # A parseable To: header is authoritative — match on it alone.
        # Addresses are normalized on both sides so a mixed-case entry
        # in EXCLUSIONS can't silently stop matching.
        rule_addresses = {a.lower() for a in rule["addresses"]}
        return any(addr in rule_addresses for addr in recipients)
    if isinstance(description, str):
        lowered = description.lower()
        return any(sub in lowered for sub in rule["description_substrings"])
    return False


def main() -> int:
    excluded_ids: list[str] = []
    unflagged = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, source, description, to_address, flagged FROM orders ORDER BY id"
        ).fetchall()
        with conn:
            for row in rows:
                if not any(
                    _matches(rule, row["source"], row["description"], row["to_address"])
                    for rule in EXCLUSIONS
                ):
                    continue
                excluded_ids.append(row["id"])
                # Unconditional reset so an already-unflagged row with a
                # stale flag_reason is normalized too (idempotent).
                conn.execute(
                    "UPDATE orders SET flagged = 0, flag_reason = NULL WHERE id = ?",
                    (row["id"],),
                )
                if row["flagged"]:
                    unflagged += 1
        json.dump(
            {
                "excluded_ids": excluded_ids,
                "excluded_ids_csv": ",".join(excluded_ids),
                "matched": len(excluded_ids),
                "unflagged": unflagged,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"apply-exclusions: SQLite error against {DB_PATH}: {exc}. "
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
