# State schema — `nightly-order-sync-cursor.json`

Per `coding-policy: stateful-artifacts`: every stateful artifact ships a schema document next to its owner skill. This file is that document.

## Path

`/workspace/group/state/nightly-order-sync-cursor.json` (overrideable per-process via the `NIGHTLY_ORDER_SYNC_CURSOR` env var, used by tests).

## Owner

`tessl__nightly-order-sync` (this skill). The cursor is written exclusively by `scripts/stamp-cursor.py`. No other skill writes it.

## Reader

`scripts/precheck-nightly-order-sync.py` (this skill, but reader-not-writer). Per the rule, the reader does NOT migrate; on encountering an unsupported `schema_version`, it treats the row as "no usable prior state" (fail-open: wake the agent so the next stamp restores a current cursor).

## Shape (schema_version 1)

```json
{
  "schema_version": 1,
  "last_run": "2026-05-01T03:14:07Z"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. Bump on shape change; only the owner script migrates. |
| `last_run` | string | yes | UTC ISO-8601 with trailing `Z`. The wall-clock instant the most recent successful run reached Step 2 (stamp) after check-orders completed without a technical failure. |

## Lifecycle

- **First run / fresh install** — cursor is absent. The precheck returns `wake_agent: true` with `reason: "no_cursor"`. The first successful run creates the file.
- **Steady state** — precheck reads the cursor; gates `wake_agent: false` when `now_utc - last_run` is under the precheck's `CADENCE` cap, otherwise `wake_agent: true`.
- **Run failure** — Step 1 (check-orders) hits a technical failure; Step 2 is skipped intentionally. The cursor stays at its prior value, so the next eligible cycle's precheck either keeps gating (if still inside the cap window) or wakes the agent for a retry (if the cap has elapsed).
- **Cursor corruption** — any read error (missing keys, malformed JSON, naive datetime, schema mismatch) flips the precheck to fail-open (`wake_agent: true`). The next successful run stamps a fresh cursor that self-heals the corruption.

## Migration policy

If a future shape change is needed (new field, renamed field, semantic shift on `last_run`):

1. Bump `SUPPORTED_SCHEMA` in `stamp-cursor.py` and `SUPPORTED_SCHEMA_VERSION` in the precheck.
2. The stamp script writes the new shape on its next run.
3. The precheck, observing `schema_version != supported`, treats the row as "no usable prior state" until the owner stamps the new shape — exactly the rule's prescribed reader behaviour.

Do NOT silently repurpose `last_run` to mean something different at the same `schema_version`.

## Why filesystem, not a `messages.db` table

The cursor is a single per-installation singleton with no cross-skill writers and no cross-row queries. A SQLite table would add a state-NNN migration upstream for one row of one column — infrastructure cost without infrastructure benefit — and the filesystem variant is greppable from a host shell (`cat`, `stat`) when triaging a run that didn't fire. If a future epic consolidates per-skill cursors into a shared table, this artifact migrates along with the others.
