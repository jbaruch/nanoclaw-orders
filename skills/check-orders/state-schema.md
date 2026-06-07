# check-orders state schema

Documents the stateful artifacts owned by `tessl__check-orders` per `jbaruch/coding-policy: stateful-artifacts`.

## Artifact: `orders_metadata` (sqlite, singleton kv table)

`orders_metadata` is a key-value table in `/workspace/store/messages.db` that holds cursor markers for the check-orders run. Pre-#294 these markers lived as siblings of the `orders` array in a JSON file at `/workspace/group/orders-db.json`; the orchestrator's startup migration imported them into this kv table once and renamed the source to `orders-db.json.migrated-<date>`.

### Schema

```sql
CREATE TABLE orders_metadata (
  key            TEXT PRIMARY KEY,
  value          TEXT,
  schema_version INTEGER NOT NULL DEFAULT 1
);
```

The `schema_version` column is added by the owner-skill migration in `scripts/write-orders-metadata.py` (idempotent ALTER TABLE; the orchestrator's `state-001` migration originally shipped the table without it). Every row carries an explicit `schema_version` per `jbaruch/coding-policy: stateful-artifacts`. Current value is `1` (constant `SCHEMA_VERSION` in `write-orders-metadata.py`).

Two well-known keys, both holding ISO-8601 UTC timestamps in the Z-suffixed millisecond shape (`YYYY-MM-DDTHH:MM:SS.fffZ`):

| Key | Meaning | Read by | Written by |
|-----|---------|---------|------------|
| `last_checked` | The cursor — boundary for the next `after:YYYY/MM/DD` Gmail filter. Stamped at SKILL.md Step 3 (write-ahead, immediately after the REST fetch) and again at Step 9 (success-path refresh, after Steps 4-8 finish). | `scripts/read-last-checked.py` (Step 1 informational); `scripts/fetch-order-emails.py` (Step 2, for the `after:` clause) | `scripts/write-orders-metadata.py` (Step 3 + Step 9) |
| `last_updated` | Co-stamped with `last_checked` for legacy compatibility with the pre-#294 JSON shape. No active reader inside this skill; surfaced by `skills/system-audit/scripts/system-audit.py` against the `orders` row column of the same name, NOT the kv key (the naming collision is historical). | (none in check-orders) | `scripts/write-orders-metadata.py` |

### Owner skill

`tessl__check-orders` is the single owner of the kv shape — it's the only skill that writes either key, and the only skill in this tile that reads `last_checked`. `morning-brief` reads `orders` rows (flagged-orders) but not `orders_metadata`.

### Writer / reader contract

Writers (`scripts/write-orders-metadata.py`):
- Parameter-bound `INSERT ... ON CONFLICT(key) DO UPDATE SET value = excluded.value` for each key inside a single transaction (atomicity guarantee: a crash between the two UPSERTs cannot leave the keys out of sync).
- Timestamp shape: ISO-8601 UTC with millisecond precision and `Z` suffix (matches `datetime.now(timezone.utc).isoformat(timespec="milliseconds")` with `+00:00` swapped for `Z`). Tests may inject a fixed value via the optional `argv[1]` for determinism.
- Called twice per run: Step 3 (write-ahead) and Step 9 (success-path refresh). Step 3 is mandatory after a parseable fetch result; Step 9 is mandatory on the happy path. Both invocations use the current wall-clock. The value is generated inside the script, not passed in from the caller — no read-then-write race.

Readers must tolerate missing rows (fresh database, pre-migration installs): both `read-last-checked.py` and `fetch-order-emails.py` return `None` / empty-string / no-filter on `SELECT ... WHERE key = 'last_checked'` returning zero rows. Readers may also receive a malformed value (manual hand-edit, corrupted migration); `fetch-order-emails.py` handles this by falling through to the unbounded-fetch path rather than crashing the whole skill.

### `schema_version` policy

The table predated `jbaruch/coding-policy: stateful-artifacts`' `schema_version` requirement; the column was added retroactively by the owner-skill migration in `scripts/write-orders-metadata.py`'s `_ensure_schema_version_column` helper. The migration is idempotent: first writer run after deploy detects the missing column via `PRAGMA table_info` and ALTERs it in with `NOT NULL DEFAULT 1`, which backfills every existing row to v1 in the same DDL statement; subsequent runs no-op.

Every UPSERT in `write-orders-metadata.py` now binds `schema_version` explicitly to the `SCHEMA_VERSION` constant (currently `1`), so a future bump to v2 propagates to existing rows on the next writer pass — readers never see a stale `schema_version` from a row that was last written under an older constant.

When a shape change becomes necessary:
1. Bump `SCHEMA_VERSION` in `write-orders-metadata.py` to the new value.
2. Add an upgrade branch in `_ensure_schema_version_column` (or a sibling helper) that performs the actual data/shape migration on existing rows.
3. Update this schema doc and the writer/reader contract above in lock-step.
4. Per `stateful-artifacts.md`'s Migration Policy, only the owner skill (check-orders) migrates — non-owner readers must not migrate; on encountering an old version they treat it as "no usable prior state".

### Hints, not authority

Per `stateful-artifacts.md`'s "Hints, Not Authority" clause, both readers treat `last_checked` as a last-seen snapshot, not ground truth. `fetch-order-emails.py` uses it to bound the Gmail fetch window but is robust to it being stale (overlapping with the cursor-minus-1-day buffer) or missing (unbounded fetch fallback). `read-last-checked.py`'s value flows into the agent's first-turn awareness only — no mutation downstream depends on it being non-null.
