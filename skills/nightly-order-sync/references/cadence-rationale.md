# Cadence rationale — why a 3-day cap

This wrapper was peeled off `nightly-external-sync` (`jbaruch/nanoclaw#581`) so check-orders runs in its own bounded container instead of competing for turn budget with four other syncs.

## Chosen — 3-day filesystem cadence cap

Precheck reads `/workspace/group/state/nightly-order-sync-cursor.json`. If `last_run` is missing or older than `CADENCE = 3d`, wake; otherwise skip. This preserves the effective cadence the orders check ran at inside the bundle (the bundle's own cap was 3 days).

The inner check-orders skill maintains its own Gmail `after:` cursor (`orders_metadata.last_checked`), so each run only fetches emails since the last successful fetch — a wake is cheap even when nothing new arrived.

## When to revisit

If order alerts feel stale, tighten `CADENCE` toward daily — check-orders is script-driven with no sub-agent fan-out, so a daily run is inexpensive. If `task_run_logs` shows the wrapper going `clean` every fire for weeks, leave the cap as-is.
