# Cadence rationale — why a filesystem cadence cap

This wrapper was peeled off `nightly-external-sync` (`jbaruch/nanoclaw#581`) so check-orders runs in its own bounded container instead of competing for turn budget with four other syncs.

## Chosen — filesystem cadence cap

Precheck reads `/workspace/group/state/nightly-order-sync-cursor.json`. If `last_run` is missing or older than the cadence cap (value in `scripts/precheck-nightly-order-sync.py`), wake; otherwise skip. This preserves the effective every-third-day cadence the orders check ran at inside the bundle. The cap value and the reason it sits below the cron-interval multiple that names the cadence live in the precheck's `CADENCE` comment (`jbaruch/nanoclaw#803`).

The inner check-orders skill maintains its own Gmail `after:` cursor (`orders_metadata.last_checked`), so each run only fetches emails since the last successful fetch — a wake is cheap even when nothing new arrived.

## When to revisit

If order alerts feel stale, tighten `CADENCE` toward daily — check-orders is script-driven with no sub-agent fan-out, so a daily run is inexpensive. If `task_run_logs` shows the wrapper going `clean` every fire for weeks, leave the cap as-is.
