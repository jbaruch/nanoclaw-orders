# Cadence rationale — why a filesystem cadence cap

This wrapper was peeled off `nightly-external-sync` (`jbaruch/nanoclaw#581`) so check-orders runs in its own bounded container instead of competing for turn budget with four other syncs.

## Chosen — filesystem cadence cap

Precheck reads `/workspace/group/state/nightly-order-sync-cursor.json`. If `last_run` is missing or older than the cadence cap (value in `scripts/precheck-nightly-order-sync.py`), wake; otherwise skip. This preserves the effective every-third-day cadence the orders check ran at inside the bundle.

The cap sits below the 72h "every third day" multiple of the 24h daily cron interval, not at it: the cursor stamps at run completion, so a cap at an exact multiple leaves the 3-day fire ~71.8h old and slips the run to every fourth day (`jbaruch/nanoclaw#803` / `nanoclaw-admin#353`). It stays above the 48h two-day fire so the run isn't every other day. The value and near-miss rationale live in the precheck's `CADENCE` comment.

The inner check-orders skill maintains its own Gmail `after:` cursor (`orders_metadata.last_checked`), so each run only fetches emails since the last successful fetch — a wake is cheap even when nothing new arrived.

## When to revisit

If order alerts feel stale, tighten `CADENCE` toward daily — check-orders is script-driven with no sub-agent fan-out, so a daily run is inexpensive. If `task_run_logs` shows the wrapper going `clean` every fire for weeks, leave the cap as-is.
