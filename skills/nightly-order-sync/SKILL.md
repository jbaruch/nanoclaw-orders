---
name: nightly-order-sync
description: "Cadence wrapper that runs check-orders on its own schedule: fetch order emails, update orders-db, flag anomalies, emit an observable-silence cursor marker. Peeled off the nightly-external-sync bundle (jbaruch/nanoclaw#581) so orders run in a bounded container. Triggers: 'order sync', 'sync orders', 'nightly order sync', 'check my orders nightly'."
cadence: "15 6 * * * (TZ=local)"
script: "scripts/precheck-nightly-order-sync.py"
---

# Nightly Order Sync

Process steps in order. Do not skip ahead.

Run this wrapper silently. The inner skill surfaces its own order alerts; the wrapper adds only cadence-cursor management and the observable-silence marker the silent-success watchdog reads from `task_run_logs.result`.

The fire-time precheck (`scripts/precheck-nightly-order-sync.py`) gates wake-ups by a 3-day cadence cap. See `references/cadence-rationale.md`.

## Step 1 — Check orders

`Skill(skill: "tessl__check-orders")` — fetch order emails, update `orders-db`, flag anomalies. The inner skill reports flagged items via `mcp__nanoclaw__send_message` and is otherwise silent.

Track whether the inner skill surfaced anything to chat — that drives the `clean` vs `surfaced` word in Step 3.

On *technical* failure (Gmail/fetch unreachable, script non-zero), do NOT stamp the cursor — emit `<internal>nightly-order-sync exited: inner-skill-fail</internal>` as your final turn text and finish here. The next cadence fire retries against the still-unadvanced cursor.

## Step 2 — Advance the success cursor

Reachable only if Step 1 completed without a technical failure. Run the stamp script silently — its JSON stdout is internal bookkeeping; do NOT echo it or narrate "cursor stamped" / "run complete" to chat.

```bash
python3 /home/node/.claude/skills/tessl__nightly-order-sync/scripts/stamp-cursor.py
```

Atomic-writes `/workspace/group/state/nightly-order-sync-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. The precheck reads `last_run` and gates the 3-day cadence. Stdout: `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`. Proceed to Step 3.

## Step 3 — Observable-silence marker

Your entire final turn is EXACTLY the one `<internal>` line below — no preceding prose, status report, or narration of Steps 1-2. This is the marker the silent-success watchdog reads from `task_run_logs.result` to tell healthy quiet from broken-silently runs: `<internal>nightly-order-sync ran <slot_key>: clean</internal>` when Step 1 surfaced nothing, or `<internal>nightly-order-sync ran <slot_key>: surfaced</internal>` when it sent at least one order alert. `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. Finish here.
