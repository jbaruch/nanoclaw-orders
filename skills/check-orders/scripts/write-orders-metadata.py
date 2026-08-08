#!/usr/bin/env python3
"""Stamp orders_metadata last_checked / last_updated with current UTC.

Called from check-orders SKILL.md at TWO points: Step 3 (write-ahead,
right after the REST fetch returns parseable JSON) and Step 11
(success-path refresh, after Steps 4-8 complete). Both invocations
use the same UPSERT shape — Step 3 stamps the fetch boundary so a
mid-run kill still advances the cursor; Step 11 refines to the
"fully processed through" boundary on the happy path. Both keys get
the same ISO-8601 timestamp via parameter-bound UPSERT. Extracted
from the SKILL.md inline SQL block per `coding-policy:
script-delegation` (deterministic operations live in script files,
not in SKILL prose).

Args (optional): a single ISO-8601 timestamp string to stamp instead
of `now`. Tests inject a fixed value to make assertions deterministic.

Stdout on success: `{"last_checked": "<iso>", "last_updated": "<iso>"}`.

Exit codes: 0 success, 1 IO/schema error, 2 CLI usage error
(timestamp arg present but not a parseable ISO-8601 string).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get("ORDERS_DB_PATH", "/workspace/store/messages.db")

# Current shape version for `orders_metadata` per
# `skills/check-orders/state-schema.md`. Owner-skill migration policy
# from `jbaruch/coding-policy: stateful-artifacts` lives here: the
# writer is the migration site. If/when v2 ships, bump this constant,
# add an upgrade branch in `_ensure_schema_version_column` (or a sibling
# helper), and update the schema doc in lock-step.
SCHEMA_VERSION = 1


def _ensure_schema_version_column(conn: sqlite3.Connection) -> None:
    """Idempotently add the `schema_version` column to `orders_metadata`.

    The orchestrator's `state-001` migration originally created the
    table without this column; `stateful-artifacts.md` requires a
    `schema_version` field on every record so migrations are auditable.
    The owner skill (check-orders) takes responsibility for the
    migration via this writer — readers stay tolerant and don't need
    the column to function.

    First write after deploy: column absent → ALTER adds it with
    `NOT NULL DEFAULT 1`, backfilling existing rows in the same
    statement. Subsequent writes: column present → no-op.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(orders_metadata)")]
    if "schema_version" in columns:
        return
    conn.execute("ALTER TABLE orders_metadata ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")


def _resolve_timestamp() -> str:
    if len(sys.argv) == 1:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if len(sys.argv) != 2:
        sys.stderr.write(f"Usage: {sys.argv[0]} [<iso-8601-timestamp>]\n")
        sys.exit(2)
    candidate = sys.argv[1]
    try:
        # Validate parseability — we still write the original literal
        # so callers can pass any format their downstream consumers
        # expect, as long as it's a real ISO-8601 string.
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        sys.stderr.write(
            f"write-orders-metadata: invalid ISO-8601 timestamp {candidate!r}: {exc}\n"
        )
        sys.exit(2)
    return candidate


def main() -> int:
    timestamp = _resolve_timestamp()
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        # Owner-skill migration: ensure the schema_version column exists
        # before any UPSERT (idempotent, see helper docstring).
        _ensure_schema_version_column(conn)
        # Single transaction so a crash between the two upserts can't
        # leave last_checked and last_updated out of sync. The
        # `schema_version` column is explicitly bound on every UPSERT
        # so existing rows are bumped to the current version on every
        # successful run (covers the case where a future migration
        # raises the constant — readers will see the correct version
        # on the next writer pass).
        conn.execute(
            "INSERT INTO orders_metadata (key, value, schema_version) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, schema_version = excluded.schema_version",
            ("last_checked", timestamp, SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT INTO orders_metadata (key, value, schema_version) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, schema_version = excluded.schema_version",
            ("last_updated", timestamp, SCHEMA_VERSION),
        )
        conn.commit()
        json.dump(
            {"last_checked": timestamp, "last_updated": timestamp},
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0
    except sqlite3.Error as exc:
        sys.stderr.write(
            f"write-orders-metadata: SQLite error against {DB_PATH}: "
            f"{exc}. Verify the database file exists, is writable, and "
            f"the orders_metadata table is present (created by the "
            f"orchestrator's state-001 migration).\n"
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
