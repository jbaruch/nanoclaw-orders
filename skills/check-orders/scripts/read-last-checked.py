#!/usr/bin/env python3
"""Read the orders_metadata.last_checked marker.

Step 1 of check-orders SKILL.md. Extracted from inline SKILL prose
per `coding-policy: script-delegation` (deterministic DB reads live
in script files, not in SKILL prose).

Stdout on success: a single JSON object `{"last_checked": "<iso>" | null}`.
The value is `null` when the row doesn't exist (fresh database) — the
caller distinguishes "marker absent" from "marker is the empty string"
without parsing the empty stdout that the original inline `sqlite3`
read produced.

Exit codes: 0 success, 1 IO/schema error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")


def main() -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT value FROM orders_metadata WHERE key = 'last_checked'"
        ).fetchone()
        last_checked = row[0] if row is not None else None
        json.dump({"last_checked": last_checked}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"read-last-checked: SQLite error reading orders_metadata "
            f"from {DB_PATH}: {exc}. Verify the database file exists "
            f"and the orders_metadata table is present (created by the "
            f"orchestrator's state-001 migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
